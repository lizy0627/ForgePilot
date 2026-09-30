"""Local text ingestion. User uploads are data, never executable paths."""
import hashlib
import json
import random
import re
import base64
import io
from pathlib import PurePosixPath
import zipfile
from xml.etree import ElementTree

TEXT_EXTENSIONS={'.txt','.md','.cpp','.cc','.cxx','.c','.h','.hpp','.hxx','.qml','.qss','.py','.jsonl'}


def extract_file(entry,notes):
    name=str(entry.get('name','材料'))[:160]
    suffix=PurePosixPath(name).suffix.lower()
    if suffix not in TEXT_EXTENSIONS|{'.pdf','.docx'}: raise ValueError(f'{name}：请上传文本、源码、PDF、DOCX 或问答 JSONL。')
    if suffix in ('.pdf','.docx'):
        try:
            encoded=entry.get('base64','')
            if not isinstance(encoded,str) or len(encoded)>12*1024*1024: raise ValueError()
            raw=base64.b64decode(encoded,validate=True)
            if not raw or len(raw)>8*1024*1024: raise ValueError()
        except (ValueError,TypeError): raise ValueError(f'{name}：文件内容无效或超过 8 MB。') from None
        try:
            if suffix=='.docx':
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    info=archive.getinfo('word/document.xml')
                    if info.file_size>16*1024*1024: raise ValueError('解压后的正文超过 16 MB，请拆分文档。')
                    root=ElementTree.fromstring(archive.read(info))
                ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                text='\n\n'.join(''.join((e.text or '') if e.tag==ns+'t' else ('\n' if e.tag==ns+'br' else '\t')
                    for e in p.iter() if e.tag in (ns+'t',ns+'br',ns+'tab')) for p in root.iter(ns+'p'))
                notes.append(f'{name}：只导入正文和表格文字，不包含图片、批注或附件。')
            else:
                from pypdf import PdfReader
                reader=PdfReader(io.BytesIO(raw))
                if reader.is_encrypted: raise ValueError('PDF 有密码，请先另存为不加密文件。')
                if len(reader.pages)>100: raise ValueError('PDF 最多 100 页，请先拆分。')
                pages=[]; empty=0
                for page in reader.pages:
                    content=page.get_contents()
                    if content is not None and len(content.get_data())>8*1024*1024: raise ValueError('PDF 页面过于复杂，请转存为文本。')
                    page_text=page.extract_text() or ''
                    if not page_text.strip(): empty+=1
                    pages.append(page_text)
                    if sum(map(len,pages))>4*1024*1024: raise ValueError('提取文字过多，请拆分文件。')
                text='\n\n'.join(pages)
                notes.append(f'{name}：已提取 {len(pages)-empty}/{len(pages)} 页文字；请检查代码缩进和排版。')
                if empty: notes.append(f'{name}：{empty} 页没有可提取文字，未参与训练；扫描图片需要先做 OCR。')
        except ValueError: raise
        except Exception as exc: raise ValueError(f'{name}：文档无法读取，请重新另存为 PDF / DOCX 或 UTF-8 文本。') from exc
        if not text.strip(): raise ValueError(f'{name}：没有可读取的文字。扫描件或图片需要先识别成文字。')
    else: text=entry.get('text','')
    if not isinstance(text,str) or len(text.encode('utf-8'))>8*1024*1024: raise ValueError(f'{name}：文字内容不能超过 8 MB。')
    if '\x00' in text: raise ValueError(f'{name}：请上传 UTF-8 文本，不要上传二进制文件。')
    return name,text.lstrip('\ufeff'),suffix


def text_segments(text):
    paragraphs=[p.strip() for p in re.split(r'\n\s*\n',text) if p.strip()]
    limit=min(1200,max(100,len(text)//12)) if len(paragraphs)<10 else 1200
    result=[]
    for paragraph in paragraphs:
        while len(paragraph)>limit:
            boundary=paragraph.rfind('\n',limit//2,limit)
            if boundary<0: boundary=limit
            result.append(paragraph[:boundary].strip()); paragraph=paragraph[boundary:].strip()
        if paragraph: result.append(paragraph)
    return result


def parse_files(files,progress=None):
    if not isinstance(files,list) or not 1 <= len(files) <= 32:
        raise ValueError('一次请选择 1–32 个文件。')
    documents=[]; kinds=set(); seen=set(); duplicate=0; notes=[]; file_info=[]
    for index,entry in enumerate(files):
        if not isinstance(entry,dict): raise ValueError('文件内容格式无效。')
        if progress: progress(index,len(files),f'正在读取：{str(entry.get("name","材料"))[:160]}')
        name,text,suffix=extract_file(entry,notes)
        if suffix=='.jsonl':
            kind='qa'; entries=[]
            for index,line in enumerate(text.splitlines(),1):
                if not line.strip(): continue
                try:
                    value=json.loads(line)
                    question=value.get('question',value.get('instruction'))
                    answer=value.get('answer',value.get('output'))
                    if not isinstance(question,str) or not isinstance(answer,str) or not question.strip() or not answer.strip():
                        raise ValueError()
                    entries.append({'question':question.strip(),'answer':answer.strip()})
                except (ValueError,AttributeError):
                    raise ValueError(f'{name} 第 {index} 行格式错误，需要 question 和 answer 两个文本字段。') from None
        else:
            kind='text'
            entries=[{'text':part} for part in text_segments(text)]
        file_info.append({'name':name,'characters':len(text),'segments':len(entries)})
        kinds.add(kind)
        for entry in entries:
            encoded=json.dumps(entry,ensure_ascii=False,sort_keys=True)
            if len(encoded)>50_000:
                raise ValueError('有一段材料过长。请用空行拆分为较短段落。')
            fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
            if fingerprint in seen:
                duplicate+=1; continue
            seen.add(fingerprint)
            documents.append({**entry,'source':name,'hash':fingerprint})
    if len(kinds)!=1:
        raise ValueError('普通文本和问答资料请分别导入。')
    if len(documents)<3:
        raise ValueError('资料太少：至少需要 3 段不同文字或 3 条不同问答，才能分开学习和检查。长文会自动分段。')
    if len(documents)>100_000:
        raise ValueError('首版每份资料最多 10 万段，请分批整理。')
    if len(documents)<50: notes.append('资料较少，建议先做小模型实验；不要只通过增加步数追求效果。')
    return {'kind':kinds.pop(),'documents':documents,'count':len(documents),'duplicates_removed':duplicate,'files':file_info,'notes':notes,
            'characters':sum(len(d.get('text',d.get('question','')+d.get('answer',''))) for d in documents)}


def split_documents(documents):
    groups={}
    for doc in documents: groups.setdefault(doc['source'],[]).append(doc)
    units=list(groups.values()) if len(groups)>=3 else [[d] for d in documents]
    random.Random(42).shuffle(units)
    held=max(1,int(len(units)*0.1))
    flatten=lambda values:[d for group in values for d in group]
    return {'train':flatten(units[2*held:]),'validation':flatten(units[:held]),'test':flatten(units[held:2*held])}


def demo_files(kind='text'):
    documents=[]
    for i in range(1,97):
        if kind=='qa':
            documents.append(json.dumps({'question':f'如何用 C++ 返回整数加 {i} 的结果？',
                'answer':f'int add_{i}(int value) {{ return value + {i}; }}'},ensure_ascii=False))
        else:
            documents.append(f'示例 {i}：把输入整数增加 {i}。函数接收一个整数，返回相加后的结果。\n'
                f'int add_{i}(int value) {{\n    return value + {i};\n}}\n'
                f'例如输入 {i} 时结果是 {i*2}。这是理解训练流程的简短示例。')
    return [{'name':'入门体验问答.jsonl' if kind=='qa' else '入门体验资料.txt',
             'text':('\n' if kind=='qa' else '\n\n').join(documents)}]
