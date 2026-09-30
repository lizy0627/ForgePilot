import base64
import io
from pathlib import Path
import sys
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio import Studio,create_app
from studio_data import parse_files,extract_file,text_segments


def pdf_bytes(blank=False):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter(); page=writer.add_blank_page(width=600,height=800)
    if not blank:
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        content=DecodedStreamObject(); content.set_data(('BT /F1 10 Tf 40 760 Td 16 TL\n'+'\n'.join(f'(Qt example {i}: QString::number converts an integer into text.) Tj T*' for i in range(12))+'\nET').encode())
        page[NameObject('/Contents')]=writer._add_object(content)
    output=io.BytesIO(); writer.write(output); return output.getvalue()


def docx_bytes():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+''.join(f'<w:p><w:r><w:t>Qt 示例 {i}：QString::number({i}) 将整数转换为字符串。</w:t></w:r></w:p>' for i in range(12))+'</w:body></w:document>')
    return output.getvalue()


def binary_file(name,data): return {'name':name,'base64':base64.b64encode(data).decode()}


def test_real_pdf_docx_and_source_extraction():
    pdf=parse_files([binary_file('Qt.pdf',pdf_bytes())]); assert pdf['count']>=3 and pdf['files'][0]['name']=='Qt.pdf'
    assert 'QString' in ''.join(d['text'] for d in pdf['documents'])
    word=parse_files([binary_file('Qt.docx',docx_bytes())]); assert word['count']==12
    source=parse_files([{'name':'widget.cpp','text':'\n\n'.join(f'int value_{i} = {i};' for i in range(12))}]); assert source['count']==12
    assert len(text_segments(' '.join(f'Qt article topic {i}.' for i in range(100))))>=3
    with pytest.raises(ValueError,match='没有可读取'): parse_files([binary_file('scan.pdf',pdf_bytes(blank=True))])
    with pytest.raises(ValueError): parse_files([binary_file('invalid.docx',b'not a zip')])
    with pytest.raises(ValueError): parse_files([{'name':'invalid.pdf','base64':'%%'}])


def test_upload_preview_confirm_and_failure(tmp_path):
    studio=Studio(tmp_path)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        headers={'X-Training-Token':studio.csrf}
        assert client.get('/studio.js').status_code==200
        assert client.post('/api/imports',json={}).status_code==403
        for entry,success in ((binary_file('Qt.docx',docx_bytes()),True),(binary_file('empty.pdf',pdf_bytes(blank=True)),False)):
            start=client.post('/api/imports',headers=headers,json={'files':[entry]}); assert start.status_code==200,start.text
            identity=start.json()['id']; deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                state=client.get('/api/imports/'+identity).json()
                if state['status']!='processing': break
                time.sleep(.1)
            assert state['status']==('ready' if success else 'failed'),state
            if success:
                assert state['preview']['count']==12 and len(state['preview']['samples'])==5
                assert not client.get('/api/state').json()['datasets']
                for _ in range(2):
                    saved=client.post('/api/imports/'+identity+'/save',headers=headers,json={})
                    assert saved.status_code==200
                assert len(client.get('/api/state').json()['datasets'])==1
                page=client.get('/api/datasets/'+identity+'?offset=5').json()
                assert page['offset']==5 and len(page['samples'])==5
            else: assert client.post('/api/imports/'+identity+'/save',headers=headers,json={}).status_code==400
