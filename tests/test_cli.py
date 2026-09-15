from cua.cli import _parse_inputs


def test_parse_inputs_accepts_equals_in_value():
    assert _parse_inputs(["member_id=1001", "note=a=b"])["note"] == "a=b"
