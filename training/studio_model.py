"""Small causal language model, initialized from scratch. No pretrained weights."""
from dataclasses import asdict, dataclass
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.checkpoint import checkpoint


@dataclass
class ModelConfig:
    vocab_size: int = 2048
    layers: int = 4
    width: int = 128
    heads: int = 2
    kv_heads: int = 1
    intermediate: int = 384
    context: int = 128

    def validate(self):
        if not (256 <= self.vocab_size <= 32768 and 2 <= self.layers <= 16
                and self.width in (128, 256, 384, 512, 768)
                and self.heads == self.width // 64 and self.kv_heads > 0
                and self.heads % self.kv_heads == 0 and 128 <= self.intermediate <= 4096
                and self.context in (128, 256, 512, 1024)):
            raise ValueError('模型参数组合无效。请使用推荐设置。')
        if self.parameter_count() > 140_000_000:
            raise ValueError('当前电脑版本将模型限制在 1.4 亿参数以内。')
        return self

    def parameter_count(self):
        d = self.width
        return int(self.vocab_size*d + self.layers*((2+2*self.kv_heads/self.heads)*d*d
                   +3*d*self.intermediate+2*d)+d)

    def dictionary(self):
        return asdict(self)


class Attention(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.q = nn.Linear(cfg.width, cfg.width, bias=False)
        self.k = nn.Linear(cfg.width, cfg.kv_heads*64, bias=False)
        self.v = nn.Linear(cfg.width, cfg.kv_heads*64, bias=False)
        self.output = nn.Linear(cfg.width, cfg.width, bias=False)
        self.register_buffer('freq', 1.0/(10000**(torch.arange(0,64,2).float()/64)), persistent=False)

    def rotate(self, x):
        angles = torch.outer(torch.arange(x.shape[-2], device=x.device).float(), self.freq.float())
        cosine, sine = angles.cos().to(x.dtype), angles.sin().to(x.dtype)
        even, odd = x[...,::2], x[...,1::2]
        return torch.stack((even*cosine-odd*sine, even*sine+odd*cosine), dim=-1).flatten(-2)

    def forward(self, x):
        b,t,_ = x.shape
        q = self.rotate(self.q(x).view(b,t,self.cfg.heads,64).transpose(1,2))
        k = self.rotate(self.k(x).view(b,t,self.cfg.kv_heads,64).transpose(1,2))
        v = self.v(x).view(b,t,self.cfg.kv_heads,64).transpose(1,2)
        out = F.scaled_dot_product_attention(q,k,v,is_causal=True,enable_gqa=True)
        return self.output(out.transpose(1,2).contiguous().view(b,t,self.cfg.width))


class Block(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.norm1 = nn.RMSNorm(cfg.width)
        self.norm2 = nn.RMSNorm(cfg.width)
        self.attention = Attention(cfg)
        self.gate = nn.Linear(cfg.width,cfg.intermediate,bias=False)
        self.up = nn.Linear(cfg.width,cfg.intermediate,bias=False)
        self.down = nn.Linear(cfg.intermediate,cfg.width,bias=False)

    def forward(self,x):
        x = x+self.attention(self.norm1(x))
        h = self.norm2(x)
        return x+self.down(F.silu(self.gate(h))*self.up(h))


class LanguageModel(nn.Module):
    def __init__(self,cfg):
        super().__init__()
        cfg.validate()
        self.cfg = cfg
        self.embedding = nn.Embedding(cfg.vocab_size,cfg.width)
        self.blocks = nn.ModuleList(Block(cfg) for _ in range(cfg.layers))
        self.norm = nn.RMSNorm(cfg.width)
        self.gradient_checkpointing = True
        self.apply(self._initialize)

    @staticmethod
    def _initialize(module):
        if isinstance(module,(nn.Linear,nn.Embedding)):
            nn.init.normal_(module.weight, std=0.02)

    def forward(self,tokens):
        if tokens.shape[1] > self.cfg.context:
            raise ValueError('输入超过此模型的上下文长度。')
        h = self.embedding(tokens)
        for block in self.blocks:
            h = checkpoint(block,h,use_reentrant=False) if self.training and self.gradient_checkpointing else block(h)
        return F.linear(self.norm(h),self.embedding.weight)
