import hashlib
import io
import pytest
from ore.storage import S3Mirror
from ore.migrate import upgrade
from sqlalchemy import create_engine,text

class FakeS3:
    def __init__(self,corrupt=False):self.corrupt=corrupt
    def put_object(self,**data):self.body=data['Body'].read();self.metadata=data
    def get_object(self,**data):return {'Body':io.BytesIO(self.body+b'wrong' if self.corrupt else self.body)}

def test_s3_readback_and_local_original(tmp_path):
    p=tmp_path/'file.txt';p.write_bytes(b'verified original')
    a={'status':'verified','path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    result=S3Mirror('synthetic-bucket',client=FakeS3()).copy(a)
    assert result['verification']=='readback_sha256' and p.read_bytes()==b'verified original'
    with pytest.raises(ValueError,match='read-back'):S3Mirror('synthetic-bucket',client=FakeS3(True)).copy(a)

def test_initial_migration_is_repeatable(tmp_path):
    url='sqlite:///'+str(tmp_path/'migrate.db');upgrade(url);upgrade(url)
    engine=create_engine(url)
    with engine.connect() as c:assert c.execute(text('SELECT version_num FROM alembic_version')).scalar()=='0002'
    engine.dispose()
