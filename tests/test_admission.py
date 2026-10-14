import io
import json
import unittest

from n150_runner.admission import AdmissionError, HostMetrics, admit, model_capacity
from n150_runner.config import load_config

CONFIG = load_config("config/pools.json")
KVM = next(p for p in CONFIG.pools if p.backend == "kvm")
GENERAL = next(p for p in CONFIG.pools if p.id == "general")
PI = next(p for p in CONFIG.pools if p.id == "pi")


class Response:
    def __init__(self, body):
        self.body = body.encode() if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        return self.body[:limit]


class AdmissionTest(unittest.TestCase):
    def test_kvm_idle_busy_recovery_and_disk(self):
        metrics = lambda load: HostMetrics(cpu_count=4, load_1m=load, mem_available_mb=12000)
        counts = {"control": 1, "pi": 1}
        check = lambda load, disk: admit(KVM, CONFIG.host, metrics(load),
                           counts=counts, pools=CONFIG.pools,
                           model_available=8, kvm_disk_free_mb=disk)
        self.assertEqual("ALLOW", check(0.5, 30000))
        self.assertEqual("KVM_CPU_HEADROOM", check(2, 30000))
        self.assertEqual("ALLOW", check(0.7, 30000))
        self.assertEqual("KVM_DISK_RESERVE", check(0.7, 1000))

    def test_memory_and_total_capacity(self):
        self.assertEqual("HOST_MEMORY_RESERVE",
            admit(GENERAL, CONFIG.host, HostMetrics(4, .5, 3200),
                  counts={"control": 1}, pools=CONFIG.pools, model_available=0))
        self.assertEqual("GLOBAL_EPHEMERAL_LIMIT",
            admit(GENERAL, CONFIG.host, HostMetrics(4, .5, 16000),
                  counts={"control": 1, "pi": 4, "recursive-coder-kvm": 1, "general": 1},
                  pools=CONFIG.pools, model_available=0))
        self.assertEqual("MODEL_CAPACITY",
            admit(PI, CONFIG.host, HostMetrics(4, .5, 16000),
                  counts={"control": 1}, pools=CONFIG.pools, model_available=0))

    def test_llama_slots_reserve_sessions(self):
        host = CONFIG.host
        data = json.dumps([{"is_processing": True}, {"is_processing": False},
                           {"is_processing": False}, {"is_processing": False}])
        value = model_capacity(host, 2, opener=lambda url, timeout: Response(data),
                      environ={"MODEL_STATUS_URL": "http://example.test/slots"})
        self.assertEqual(2, value)

    def test_vllm_waiting_and_bad_samples_fail_closed(self):
        host = CONFIG.host
        self.assertEqual(0, model_capacity(host, 0,
                         opener=lambda url, timeout: Response("vllm:num_requests_running 1\nvllm:num_requests_waiting 2\n"),
                         environ={"MODEL_STATUS_URL": "http://example.test/metrics"}))
        for bad in ('{"nonsense":true}', "vllm:num_requests_running 1\n", "foo 17"):
            with self.subTest(bad=bad), self.assertRaises(AdmissionError):
                model_capacity(host, 0, opener=lambda url, timeout: Response(bad),
                               environ={"MODEL_STATUS_URL": "http://example.test/metrics"})


if __name__ == "__main__":
    unittest.main()
