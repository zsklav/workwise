"""Bounded closed-loop HTTP measurements using pooled persistent connections."""
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from math import ceil
from time import perf_counter

import httpx


def percentile(values, fraction):
    if not values or not 0 < fraction <= 1:
        raise ValueError("Percentiles require samples and a fraction in (0,1]")
    ordered = sorted(values)
    return ordered[ceil(len(ordered) * fraction) - 1]


def statistics(samples, elapsed):
    if not samples or elapsed <= 0:
        raise ValueError("Nonempty samples and positive elapsed time required")
    latencies = [sample["seconds"] * 1000 for sample in samples]
    failures = sum(not sample["ok"] for sample in samples)
    return {"requests": len(samples), "failures": failures, "failure_rate": failures / len(samples),
            "elapsed_seconds": elapsed, "requests_per_second": len(samples) / elapsed,
            "successful_requests_per_second": (len(samples) - failures) / elapsed,
            "latency_ms": {"p50":percentile(latencies,.5), "p95":percentile(latencies,.95),
                           "p99":percentile(latencies,.99), "max":max(latencies)},
            "statuses":dict(Counter(str(sample["status"]) for sample in samples)),
            "errors":dict(Counter(sample["error"] for sample in samples if sample["error"]))}


@dataclass
class Workload:
    name: str
    path: str
    method: str = "GET"
    params: dict = field(default_factory=dict)
    payload: dict | None = None
    company_id: int | None = None
    company_name: str | None = None

    def valid(self, response):
        if response.status_code != (201 if self.method == "POST" else 200):
            return False
        value = response.json()
        if self.name == "job_detail" or self.method == "POST":
            return isinstance(value,dict) and type(value.get("id")) is int and value.get("version",0) > 0 and (self.company_id is None or value.get("company_id") == self.company_id)
        if not isinstance(value,list) or not 0 < len(value) <= 20:
            return False
        ids = [row["id"] for row in value]
        return ids == sorted(set(ids)) and all(
            (self.company_id is None or row["company_id"] == self.company_id)
            and (self.company_name is None or row["company"] == self.company_name) for row in value)


async def measure(client, workload, *, requests, concurrency, token=None):
    if not 1 <= concurrency <= 32 or not 1 <= requests <= 10000:
        raise ValueError("Use 1..32 clients and 1..10000 requests per round")
    samples = []
    positions = iter(range(requests))
    async def worker():
        for _ in positions:
            started = perf_counter()
            status, error, ok = "transport_error", None, False
            try:
                response = await client.request(workload.method,workload.path,params=workload.params,
                    json=workload.payload,headers={"Authorization":"Bearer "+token} if workload.method == "POST" and token else {})
                seconds = perf_counter() - started
                status = response.status_code
                try:
                    ok = workload.valid(response)
                except (ValueError,KeyError,TypeError):
                    ok = False
                if not ok:
                    error = "UnexpectedResponse"
            except httpx.HTTPError as exception:
                seconds = perf_counter() - started
                error = type(exception).__name__
            samples.append({"seconds":seconds,"status":status,"ok":ok,"error":error})
    started = perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return statistics(samples,perf_counter()-started)


def workloads(fixture):
    company_id = fixture["company_id"]
    return [
        Workload("job_detail",f"/jobs/{fixture['job_id']}"),
        Workload("list_first_page","/jobs",params={"limit":20}),
        Workload("company_id_first_page","/jobs",params={"company_id":company_id,"limit":20},company_id=company_id),
        Workload("company_name_first_page","/jobs",params={"company":fixture["company_name"],"limit":20},company_name=fixture["company_name"]),
        Workload("deep_offset","/jobs",params={"offset":fixture["jobs"]-20,"limit":20}),
        Workload("create_job","/jobs",method="POST",payload={"title":"Measured create","company_id":company_id,"location":"Remote"},company_id=company_id),
    ]


async def run_suite(base_url, fixture, token, *, requests=100, clients=(1,8), repeats=2, warmup=10, transport=None):
    if not 1 <= repeats <= 5 or not 0 <= warmup <= 100 or not clients or len(clients)>4 or len(set(clients)) != len(clients):
        raise ValueError("Use 1..5 repeats, 0..100 warmup calls and 1..4 distinct concurrency levels")
    if not 1 <= requests <= 10000 or any(not 1 <= count <= 32 for count in clients):
        raise ValueError("Invalid request count or concurrency")
    if (requests + warmup) * len(clients) * repeats * 6 > 50000:
        raise ValueError("Total request budget including warmup must not exceed 50000")
    rounds = []
    for workload in workloads(fixture):
        for concurrency in clients:
            for repeat in range(1,repeats+1):
                limits = httpx.Limits(max_connections=concurrency,max_keepalive_connections=concurrency)
                async with httpx.AsyncClient(base_url=base_url,limits=limits,timeout=httpx.Timeout(10,connect=3),
                                             follow_redirects=False,trust_env=False,transport=transport) as client:
                    if warmup:
                        preflight = await measure(client,workload,requests=warmup,concurrency=concurrency,token=token)
                        if preflight["failures"]:
                            raise RuntimeError(f"Warmup failed for {workload.name}")
                    result = await measure(client,workload,requests=requests,concurrency=concurrency,token=token)
                row={"scenario":workload.name,"clients":concurrency,"repeat":repeat,**result}
                rounds.append(row)
                print(f"Measured {workload.name}: clients={concurrency}, repeat={repeat}, p95={result['latency_ms']['p95']:.2f}ms, requests/s={result['requests_per_second']:.2f}, failures={result['failures']}",flush=True)
    return rounds
