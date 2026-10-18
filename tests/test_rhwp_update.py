from hwp_cowork.rhwp_update import assess

TAGS = ["v0.9.1", "v0.9.0", "v0.8.9", "v0.8.7", "v0.8.6", "v0.8.4"]


def test_up_to_date():
    st = assess("v0.8.6", ["v0.8.6", "v0.8.4"])
    assert st.behind == 0 and st.warning is None


def test_one_patch_behind_is_quiet():
    st = assess("v0.8.6", ["v0.8.7", "v0.8.6"])
    assert st.behind == 1 and st.warning is None


def test_many_behind_and_minor_bump_warn():
    st = assess("v0.8.6", TAGS)
    assert st.behind == 4 and st.latest == "v0.9.1"
    assert "4개" in st.warning and "부 버전" in st.warning


def test_minor_bump_alone_warns():
    st = assess("v0.8.6", ["v0.9.0", "v0.8.6"])
    assert st.behind == 1 and "부 버전" in st.warning
