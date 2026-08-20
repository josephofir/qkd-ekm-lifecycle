from qkd_ekm.common.log import LINE_RE, get_logger


def test_format(capsys):
    get_logger("EKM").info("Got new key request for instance QKD2")
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert LINE_RE.match(out), out
    assert out.endswith("EKM: Got new key request for instance QKD2")
