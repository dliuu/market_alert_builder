"""M0 smoke tests: the worker imports, its CLI exists, and the generated
contract binding is importable (proves contracts:gen produced valid Python)."""


def test_cli_has_entrypoint() -> None:
    from worker import cli

    assert hasattr(cli, "main")


def test_generated_contract_imports() -> None:
    import contracts.brief as brief

    # BriefObject is the top-level model generated from the JSON Schema title.
    assert hasattr(brief, "BriefObject")
