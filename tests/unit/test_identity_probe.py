from local_agent import provenance


def test_identity_probe():
    actual = provenance.source_sha256()
    raise AssertionError(f"COMPUTED_SOURCE_SHA256={actual} BASE_PROMPT_SHA256={provenance.base_prompt_sha256()}")
