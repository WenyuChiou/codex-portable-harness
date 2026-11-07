import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''import json,sys,os,time,subprocess
for line in sys.stdin:
    m=json.loads(line)
    if 'id' not in m: continue
    if m['method']=='initialize':
        result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif m.get('params',{}).get('name')=='hang':
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
        open(sys.argv[1],'w').write(str(child.pid))
        time.sleep(120)
    else: result={'content':[{'type':'text','text':str(os.getpid())}]}
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake=Path(self.tmp.name)/'fake.py'
        self.child_path=Path(self.tmp.name)/'child.pid'
        fake.write_text(FAKE)
        self.p=subprocess.Popen([sys.executable,str(ROOT/'scripts/mcp_lifecycle.py'),'--query-seconds','.3','--startup-seconds','3','--',sys.executable,str(fake),str(self.child_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        self.q=queue.Queue()
        threading.Thread(target=lambda: [self.q.put(line) for line in self.p.stdout],daemon=True).start()
        self.addCleanup(self.close)
        response=self.call(1,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        self.assertIn('result',response)

    def close(self):
        if self.p.stdin and not self.p.stdin.closed:
            self.p.stdin.close()
        try: self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=5)
        for stream in (self.p.stdout,self.p.stderr):
            if stream: stream.close()

    def call(self,id,method,params):
        self.p.stdin.write(json.dumps(dict(jsonrpc='2.0',id=id,method=method,params=params))+'\n')
        self.p.stdin.flush()
        return json.loads(self.q.get(timeout=8))

    def test_backend_exits_after_response_and_next_request_works(self):
        first=self.call(2,'tools/call',{'name':'read'})
        second=self.call(3,'tools/call',{'name':'read'})
        self.assertNotEqual(first['result']['content'][0]['text'],second['result']['content'][0]['text'])

    def test_timeout_returns_error_and_next_call_works_without_replaying(self):
        response=self.call(2,'tools/call',{'name':'hang'})
        self.assertIn('error',response)
        self.assertIn('result',self.call(3,'tools/call',{'name':'read'}))
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            api=ctypes.WinDLL('kernel32',use_last_error=True)
            api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            api.OpenProcess.restype=wintypes.HANDLE
            api.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            api.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=api.OpenProcess(0x1000,False,int(self.child_path.read_text()))
            if handle:
                code=wintypes.DWORD()
                self.assertTrue(api.GetExitCodeProcess(handle,ctypes.byref(code)))
                api.CloseHandle(handle)
                self.assertNotEqual(code.value,259,'timed-out backend left a live child')

    def test_client_eof_exits_proxy(self):
        self.p.stdin.close()
        self.assertEqual(self.p.wait(timeout=5),0)


if __name__ == '__main__':
    unittest.main()
