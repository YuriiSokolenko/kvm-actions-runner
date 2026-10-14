"""Policy helpers shared by the KVM deployment verifier and regression tests."""

EXPECTED_NANO_CPUS = 2_250_000_000
# Docker can lose a few nanocpus when converting --cpus to NanoCpus. Permit at
# most 0.001 CPU of downward rounding, and never accept a value above the cap.
NANO_CPU_ROUNDING_TOLERANCE = 1_000_000


def nano_cpus_within_limit(actual: object) -> bool:
    """Return whether Docker's reported quota is within the configured cap."""
    return (
        type(actual) is int
        and EXPECTED_NANO_CPUS - NANO_CPU_ROUNDING_TOLERANCE <= actual <= EXPECTED_NANO_CPUS
    )
