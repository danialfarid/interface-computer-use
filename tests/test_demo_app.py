from urllib.request import urlopen

from cua.demo_app import serve


def test_demo_app_renders_lookup_and_member_detail():
    server = serve(port=0)
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        with urlopen(origin + "/") as response:
            home = response.read().decode()
        with urlopen(origin + "/member?member=1001") as response:
            detail = response.read().decode()

        assert 'name="member"' in home
        assert "Member details" in detail
        assert "$1,240.50" in detail
    finally:
        server.shutdown()
        server.server_close()
