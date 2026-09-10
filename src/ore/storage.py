"""Optional S3 replication of verified local artifacts; local originals remain authoritative."""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path

class S3Mirror:
    def __init__(self,bucket,*,prefix='ore',endpoint_url=None,client=None):
        if not bucket:raise ValueError('Bucket is required')
        self.bucket=bucket;self.prefix=prefix.strip('/')
        if client is None:
            import boto3
            client=boto3.client('s3',endpoint_url=endpoint_url)
        self.client=client
    def copy(self,artifact):
        if artifact.get('status')!='verified':raise ValueError('Only verified artifacts can be replicated')
        path=Path(artifact['path']);digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=artifact['sha256']:raise ValueError('Local artifact changed since verification')
        key='/'.join(x for x in (self.prefix,digest[:2],digest) if x)
        checksum=base64.b64encode(bytes.fromhex(digest)).decode()
        with path.open('rb') as body:
            self.client.put_object(Bucket=self.bucket,Key=key,Body=body,ContentType=artifact.get('media_type','application/octet-stream'),
                Metadata={'sha256':digest},ChecksumSHA256=checksum)
        # Read back bytes rather than trusting user-writable metadata or multipart ETags.
        response=self.client.get_object(Bucket=self.bucket,Key=key)
        stream=response['Body'];remote=hashlib.sha256();count=0
        try:
            while chunk:=stream.read(1024*1024):remote.update(chunk);count+=len(chunk)
        finally:stream.close()
        if remote.hexdigest()!=digest or count!=path.stat().st_size:raise ValueError('Replicated artifact failed read-back verification')
        return {'bucket':self.bucket,'key':key,'sha256':digest,'bytes':count,'status':'verified','verification':'readback_sha256'}
