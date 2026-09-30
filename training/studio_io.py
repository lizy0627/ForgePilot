import json
from pathlib import Path
from uuid import uuid4


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path,value):
    path=Path(path)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)
