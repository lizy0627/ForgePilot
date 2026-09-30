"""Owned worker processes: training and inference never run in the Agent process."""
import argparse
from contextlib import nullcontext
import json
import importlib.util
import math
import os
from pathlib import Path
import shutil
import sys
import time
from datetime import datetime,timezone

from studio_io import read_json,write_json

SPECIAL=['<|pad|>','<|bos|>','<|eos|>','<|user|>','<|assistant|>','<|system|>','<|end|>']


def load_architecture(directory):
    """Load the source saved by this studio, keeping old models reproducible."""
    source=directory/'studio_model.py'
    if not source.exists(): source=Path(__file__).with_name('studio_model.py')
    spec=importlib.util.spec_from_file_location('forgepilot_saved_architecture',source)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module; spec.loader.exec_module(module)
    return module.LanguageModel,module.ModelConfig


def train(directory):
    # Imports stay in the GPU worker, so opening the interface allocates no GPU memory.
    import numpy as np
    import torch
    from tokenizers import Tokenizer,models,pre_tokenizers,decoders,trainers
    from safetensors.torch import save_file,load_file
    from studio_data import split_documents

    request=read_json(directory/'request.json'); dataset=read_json(directory/'dataset.json')
    state=read_json(directory/'state.json')
    def phase(name,message):
        state.update(phase=name,message=message)
        state.setdefault('events',[]).append({'time':datetime.now(timezone.utc).isoformat(),'message':message})
        state['events']=state['events'][-100:]; write_json(directory/'state.json',state)
    state.update(status='preparing',message='正在准备分词器和模型。',worker_pid=os.getpid())
    write_json(directory/'state.json',state)
    phase('prepare','正在划分学习、验证和测试资料。')
    if not torch.cuda.is_available(): raise ValueError('没有检测到可用显卡，请先运行环境检查。')
    torch.set_num_threads(2)
    torch.cuda.set_per_process_memory_fraction(0.60)
    torch.manual_seed(42); torch.cuda.manual_seed_all(42)
    device='cuda'; bf16=torch.cuda.is_bf16_supported()
    autocast=lambda:torch.autocast('cuda',dtype=torch.bfloat16) if bf16 else nullcontext()
    split=split_documents(dataset['documents'])
    mode=request['mode']; artifact=directory/'model'; artifact.mkdir(exist_ok=True)
    if not (artifact/'studio_model.py').exists():
        source=Path(request['base_model'])/'studio_model.py' if request.get('base_model') else Path(__file__).with_name('studio_model.py')
        if not source.exists(): source=Path(__file__).with_name('studio_model.py')
        shutil.copyfile(source,artifact/'studio_model.py')
    LanguageModel,ModelConfig=load_architecture(artifact)
    phase('tokenize','正在准备分词器和模型结构。')
    if request.get('base_model'):
        base=Path(request['base_model'])
        tokenizer=Tokenizer.from_file(str(base/'tokenizer.json'))
        cfg=ModelConfig(**read_json(base/'config.json')).validate()
    elif (artifact/'tokenizer.json').exists():
        tokenizer=Tokenizer.from_file(str(artifact/'tokenizer.json'))
        cfg=ModelConfig(**read_json(artifact/'config.json')).validate()
    else:
        tokenizer=Tokenizer(models.BPE(unk_token=None))
        tokenizer.pre_tokenizer=pre_tokenizers.ByteLevel(add_prefix_space=False)
        tokenizer.decoder=decoders.ByteLevel()
        tokenizer.train_from_iterator((d['text'] for d in split['train']),trainers.BpeTrainer(
            vocab_size=request['vocab_size'],min_frequency=2,special_tokens=SPECIAL,
            initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),show_progress=False))
        cfg=ModelConfig(**request['architecture'],vocab_size=tokenizer.get_vocab_size()).validate()
    tokenizer.save(str(artifact/'tokenizer.json')); write_json(artifact/'config.json',cfg.dictionary())
    pad=tokenizer.token_to_id('<|pad|>'); eos=tokenizer.token_to_id('<|eos|>')
    model=LanguageModel(cfg).to(device)
    if sum(p.numel() for p in model.parameters()) != cfg.parameter_count():
        raise ValueError('模型参数量检查失败。')
    if request.get('base_model'): model.load_state_dict(load_file(str(Path(request['base_model'])/'model.safetensors')))
    decay=[p for p in model.parameters() if p.ndim>=2]
    no_decay=[p for p in model.parameters() if p.ndim<2]
    optimizer=torch.optim.AdamW([{'params':decay,'weight_decay':0.1},{'params':no_decay,'weight_decay':0}],
        lr=request['learning_rate'],betas=(0.9,0.95))
    generator=torch.Generator().manual_seed(42)
    token_data={}; example_data={}; data_counts={}
    for partition,docs in split.items():
        if mode!='sft':
            path=directory/f'{partition}.bin'
            if not path.exists():
                with path.open('wb') as stream:
                    for doc in docs:
                        np.asarray(tokenizer.encode(doc['text']).ids+[eos],dtype=np.uint32).tofile(stream)
            token_data[partition]=np.memmap(path,dtype=np.uint32,mode='r')
            data_counts[partition]=len(token_data[partition])
            if data_counts[partition]<2:
                label={'train':'学习','validation':'验证','test':'测试'}[partition]
                raise ValueError(f'{label}资料不足，请增加不同的文字和代码段落。')
        else:
            examples=[]
            for doc in docs:
                prefix=tokenizer.encode('<|user|>'+doc['question']+'<|assistant|>').ids
                answer=tokenizer.encode(doc['answer']+'<|end|>').ids
                ids=prefix+answer
                if len(ids)>cfg.context+1:
                    raise ValueError('有问答超过基础模型的上下文，请缩短 question/answer 后重新导入。')
                target=[-100]*len(prefix)+answer
                ids=ids+[pad]*(cfg.context+1-len(ids))
                target=target+[-100]*(cfg.context+1-len(target))
                examples.append((ids[:-1],target[1:]))
            example_data[partition]=examples
            data_counts[partition]=sum(sum(t!=-100 for t in y) for _,y in examples)
    def batch(partition,rng):
        if mode!='sft':
            data=token_data[partition]
            length=min(cfg.context,len(data)-1)
            start=int(torch.randint(len(data)-length,(1,),generator=rng).item())
            values=torch.from_numpy(np.asarray(data[start:start+length+1],dtype=np.int64)).unsqueeze(0).to(device)
            return values[:,:-1],values[:,1:]
        index=int(torch.randint(len(example_data[partition]),(1,),generator=rng).item())
        x,y=example_data[partition][index]
        return torch.tensor([x],device=device),torch.tensor([y],device=device)
    def loss_for(x,y):
        with autocast():
            logits=model(x)
            return torch.nn.functional.cross_entropy(logits.float().reshape(-1,cfg.vocab_size),y.reshape(-1),ignore_index=-100)
    @torch.no_grad()
    def evaluate(partition):
        model.eval(); rng=torch.Generator().manual_seed(2026); losses=[]
        for _ in range(3):
            losses.append(float(loss_for(*batch(partition,rng))))
        model.train(); return sum(losses)/len(losses)

    checkpoint_path=directory/'checkpoint.pt'
    step=0; tokens=0; elapsed_before=0; metrics=[]; best=None
    if checkpoint_path.exists():
        saved=torch.load(checkpoint_path,map_location='cpu',weights_only=True)
        model.load_state_dict(saved['model']); optimizer.load_state_dict(saved['optimizer'])
        generator.set_state(saved['sample_rng']); torch.set_rng_state(saved['cpu_rng']); torch.cuda.set_rng_state_all(saved['cuda_rng'])
        step=saved['step']; tokens=saved['tokens']; metrics=saved['metrics']; best=saved['best']
        elapsed_before=saved['elapsed_seconds']
    state.update(status='running',message='正在训练。可以随时暂停并稍后继续。',parameters=cfg.parameter_count(),
        actual_vocab=cfg.vocab_size,context=cfg.context,split_counts={k:len(v) for k,v in split.items()},
        token_counts=data_counts,step=step,metrics=metrics,mode=mode)
    write_json(directory/'state.json',state)
    phase('train','资料准备完成，开始更新模型参数。' if step==0 else f'已恢复第 {step} 步，继续训练。')
    started=time.perf_counter(); total=request['steps']; accumulator=request['accumulation']; last_val=state.get('validation_loss')
    def update(status='running',message=None):
        elapsed=elapsed_before+time.perf_counter()-started
        state.update(status=status,step=step,total_steps=total,tokens=tokens,metrics=metrics[-400:],
            elapsed_seconds=elapsed,validation_loss=last_val,best_validation_loss=best,
            tokens_per_second=round(tokens/max(elapsed,0.01)),
            estimated_seconds_remaining=round((total-step)*elapsed/max(step,1)),
            repetitions=round(tokens/max(1,data_counts['train']),2),
            gpu_memory_mib=round(torch.cuda.max_memory_allocated()/2**20,1))
        if message: state['message']=message
        write_json(directory/'state.json',state)
    def save():
        elapsed=elapsed_before+time.perf_counter()-started
        temporary=directory/'checkpoint.pt.tmp'
        torch.save({'model':model.state_dict(),'optimizer':optimizer.state_dict(),'step':step,'tokens':tokens,
            'sample_rng':generator.get_state(),'cpu_rng':torch.get_rng_state(),'cuda_rng':torch.cuda.get_rng_state_all(),
            'elapsed_seconds':elapsed,'metrics':metrics[-400:],'best':best},temporary)
        temporary.replace(checkpoint_path)
        temporary_weights=artifact/'model.safetensors.tmp'
        save_file({k:v.detach().cpu().contiguous() for k,v in model.state_dict().items()},str(temporary_weights))
        temporary_weights.replace(artifact/'model.safetensors')
        write_json(artifact/'training.json',{'mode':mode,'run_id':directory.name,'step':step,'parameters':cfg.parameter_count(),
            'dataset_name':dataset['name'],'dataset_id':dataset['id'],'token_counts':data_counts,'metrics':metrics[-400:],
            'base_run':request.get('base_run'),'architecture':'ForgePilot custom causal Transformer',
            'capabilities':['text'],'tool_calling_verified':False})
        (artifact/'MODEL_CARD.md').write_text('# 本地训练模型\n\n'
            f'训练记录：{directory.name}；模式：{mode}；已完成 {step} 步。\n\n'
            '包含自定义结构、tokenizer 和实际训练权重。小样本实验不能证明可靠代码能力。\n'
            '尚未验证工具调用、结构化计划或自动修改项目能力。\n',encoding='utf-8')
    model.train()
    if step==0: last_val=evaluate('validation')
    for next_step in range(step+1,total+1):
        command=read_json(directory/'control.json').get('action')
        if command in ('pause','stop'):
            save(); phase('paused','已保存权重和进度，正在释放显卡。'); update('paused' if command=='pause' else 'stopped'); return
        warmup=max(1,int(total*0.03))
        rate=min(1,next_step/warmup) if next_step<=warmup else 0.1+0.9*(1+math.cos(math.pi*(next_step-warmup)/max(1,total-warmup)))/2
        for group in optimizer.param_groups: group['lr']=request['learning_rate']*rate
        optimizer.zero_grad(set_to_none=True); average=0; used=0
        for _ in range(accumulator):
            x,y=batch('train',generator); loss=loss_for(x,y)
            if not bool(torch.isfinite(loss)): raise ValueError('训练出现无效数值，请降低学习率后重新训练。')
            (loss/accumulator).backward(); average+=float(loss.detach())/accumulator; used+=int((y!=-100).sum())
        norm=torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
        if not bool(torch.isfinite(norm)): raise ValueError('梯度出现无效数值，请降低学习率。')
        optimizer.step(); step=next_step; tokens+=used
        if step%10==0 or step==1 or step==total:
            last_val=evaluate('validation')
            best=last_val if best is None else min(best,last_val)
            metrics.append({'step':step,'train':average,'validation':last_val})
            state.setdefault('events',[]).append({'time':datetime.now(timezone.utc).isoformat(),'message':f'第 {step} 步：训练损失 {average:.3f}，验证损失 {last_val:.3f}。'})
            state['events']=state['events'][-100:]
        state['train_loss']=average
        if step%25==0 or step==total: save()
        update()
        if request['gentle']: time.sleep(0.12)
    phase('evaluate','训练步数完成，正在用测试资料检查模型。')
    state['test_loss']=evaluate('test')
    phase('save','正在保存权重、分词器和模型结构。')
    save(); phase('complete','模型已保存。可以试用、下载或添加新资料继续学习。'); update('completed')


def import_documents(directory):
    from studio_data import parse_files
    value=read_json(directory/'input.json'); state=read_json(directory/'state.json')
    def progress(done,total,message):
        state.update(progress=round(done/max(1,total)*100),message=message); write_json(directory/'state.json',state)
    dataset=parse_files(value['files'],progress)
    dataset['name']=str(value.get('name') or value['files'][0]['name'])[:100]
    write_json(directory/'result.json',dataset)
    state.update(status='ready',progress=100,message='文字提取和去重完成，请预览后确认。'); write_json(directory/'state.json',state)
    (directory/'input.json').unlink(missing_ok=True)


def generate(model_directory,request_path,result_path):
    import torch
    from tokenizers import Tokenizer
    from safetensors.torch import load_file
    LanguageModel,ModelConfig=load_architecture(model_directory)
    torch.set_num_threads(2)
    if not torch.cuda.is_available(): raise ValueError('显卡不可用。')
    torch.cuda.set_per_process_memory_fraction(0.60)
    request=read_json(request_path); cfg=ModelConfig(**read_json(model_directory/'config.json'))
    tokenizer=Tokenizer.from_file(str(model_directory/'tokenizer.json'))
    model=LanguageModel(cfg).cuda().eval(); model.load_state_dict(load_file(str(model_directory/'model.safetensors')))
    metadata=read_json(model_directory/'training.json')
    if metadata['mode']=='sft':
        text=''
        for message in request['messages']:
            role=message['role']; text+=f'<|{role}|>'+message['content']+('<|end|>' if role=='assistant' else '')
        text+='<|assistant|>'
    else:
        text='\n'.join(m['content'] for m in request['messages'])
    ids=tokenizer.encode(text).ids
    if not ids or len(ids)>=cfg.context:
        raise ValueError(f'输入太长或为空。此模型最多容纳 {cfg.context} 个分词单位，请缩短输入。')
    limit=min(request.get('max_tokens',80),cfg.context-len(ids)); generated=[]
    stops={tokenizer.token_to_id('<|eos|>'),tokenizer.token_to_id('<|end|>')}
    suppressed=[tokenizer.token_to_id(s) for s in SPECIAL if s not in ('<|eos|>','<|end|>')]
    torch.manual_seed(42)
    with torch.inference_mode():
        for _ in range(limit):
            x=torch.tensor([ids+generated],device='cuda')
            with torch.autocast('cuda',dtype=torch.bfloat16,enabled=torch.cuda.is_bf16_supported()):
                logits=model(x)[0,-1].float()
            logits[suppressed]=-float('inf')
            values,indices=logits.topk(min(40,len(logits)))
            token=int(indices[torch.multinomial(torch.softmax(values/0.8,dim=-1),1)].item())
            if token in stops: break
            generated.append(token)
    write_json(result_path,{'text':tokenizer.decode(generated),'prompt_tokens':len(ids),'completion_tokens':len(generated),
        'finish_reason':'length' if len(generated)==limit else 'stop','mode':metadata['mode']})


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('action',choices=['train','generate','import']); parser.add_argument('directory',type=Path)
    parser.add_argument('--request',type=Path); parser.add_argument('--result',type=Path); args=parser.parse_args()
    try:
        if args.action=='import': import_documents(args.directory)
        elif args.action=='train': train(args.directory)
        else: generate(args.directory,args.request,args.result)
    except Exception as exc:
        message=str(exc)
        if 'out of memory' in message.lower(): message='显存不足。请关闭其他占用显卡的应用，或选择更小模型和上下文。最近保存的进度已保留。'
        if args.action in ('train','import'):
            state=read_json(args.directory/'state.json'); state.update(status='failed',message=message[:1500]); write_json(args.directory/'state.json',state)
        elif args.result: write_json(args.result,{'error':message[:1500]})
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
