import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
uas={"browser":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36","bot":"Mozilla/5.0 (compatible; economy-thermometer/1.0)","plain":"Mozilla/5.0","curl":"curl/8.5.0"}
for host in ("query1","query2"):
    for n,ua in uas.items():
        url="https://%s.finance.yahoo.com/v8/finance/chart/%%5EGSPC?period1=1072915200&period2=1793099297&interval=1mo"%host
        try:
            with urlopen(Request(url,headers={"User-Agent":ua,"Accept":"*/*"}),timeout=30) as r:print(host,n,r.status,len(r.read()))
        except HTTPError as e:print(host,n,"HTTP",e.code)
        except Exception as e:print(host,n,"ERR",e)
        time.sleep(2)
