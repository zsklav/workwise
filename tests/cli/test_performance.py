import asyncio

import httpx
import pytest

from scripts.performance.http_load import Workload, measure, percentile, run_suite, statistics
from scripts.performance.query_plan import summarize


def test_latency_percentiles_and_failed_request_accounting():
    samples=[{"seconds":i/1000,"status":200 if i<100 else 503,"ok":i<100,"error":None if i<100 else "UnexpectedResponse"} for i in range(1,101)]
    report=statistics(samples,2)
    assert report["requests"] == 100 and report["failures"] == 1
    assert report["failure_rate"] == .01
    assert report["requests_per_second"] == 50
    assert report["successful_requests_per_second"] == 49.5
    assert report["latency_ms"] == {"p50":50,"p95":95,"p99":99,"max":100}
    assert report["statuses"] == {"200":99,"503":1}
    assert report["errors"] == {"UnexpectedResponse":1}


@pytest.mark.parametrize("values,fraction",[([], .5),([1],0),([1],1.1)])
def test_invalid_percentiles_are_rejected(values,fraction):
    with pytest.raises(ValueError):
        percentile(values,fraction)


def test_excessive_total_load_fails_before_creating_clients_or_requests():
    with pytest.raises(ValueError,match="Total request budget"):
        asyncio.run(run_suite("http://unused",None,None,requests=10000,clients=(1,8),repeats=5))


def test_pooled_runner_caps_concurrency_and_counts_all_failures():
    active,maximum,attempts=0,0,0
    async def handler(request):
        nonlocal active,maximum,attempts
        active+=1
        maximum=max(maximum,active)
        index=attempts
        attempts+=1
        try:
            await asyncio.sleep(.001)
            if index == 0:
                raise httpx.ReadTimeout("do-not-report-this-private-error",request=request)
            if index == 1:
                return httpx.Response(503,json={"detail":"private-body"})
            if index == 2:
                return httpx.Response(200,json={"invalid":"body"})
            return httpx.Response(200,json={"id":7,"version":1})
        finally:
            active-=1
    async def run():
        async with httpx.AsyncClient(base_url="http://test",transport=httpx.MockTransport(handler)) as client:
            return await measure(client,Workload("job_detail","/jobs/7"),requests=20,concurrency=3)
    report=asyncio.run(run())
    assert attempts == report["requests"] == 20 and maximum == 3
    assert report["failures"] == 3
    assert report["errors"] == {"ReadTimeout":1,"UnexpectedResponse":2}
    assert "private" not in str(report)


def test_correct_status_with_wrong_filter_or_order_is_a_failure():
    workload=Workload("company_id_first_page","/jobs",company_id=7)
    for rows in ([{"id":1,"company_id":8}], [{"id":2,"company_id":7},{"id":1,"company_id":7}], []):
        assert not workload.valid(httpx.Response(200,json=rows))
    assert workload.valid(httpx.Response(200,json=[{"id":1,"company_id":7}]))


def test_plan_summary_uses_inclusive_root_buffers_and_accounts_for_loops():
    sample={"Execution Time":2,"Plan":{"Node Type":"Limit","Actual Rows":20,"Local Hit Blocks":10,
        "Plans":[{"Node Type":"Index Scan","Index Name":"fixture_idx","Local Hit Blocks":9,"Rows Removed by Filter":3,"Actual Loops":2}]}}
    result=summarize([sample,{**sample,"Execution Time":4}])
    assert result["buffer_accesses"] == 10  # Child buffers already contribute to the root.
    assert result["rows_removed_by_filter"] == 6
    assert result["median_execution_ms"] == 3
