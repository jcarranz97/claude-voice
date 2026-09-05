"""What the machine is doing.

Moved here with the code: these tests reach into the caches and the two
GPU back ends directly, which is the point of them -- the numbers are read
from files that are not there on most machines, so every path has to be
driven by a fake filesystem rather than by whatever this laptop has.
"""

import pathlib
import types

import pytest

import claude_voice.sysstat as sysstat


@pytest.fixture(autouse=True)
def fresh(home):
    """The caches back to their import-time values, as sysstat's own do."""
    sysstat._gpu_cache.update(t=0.0, val=None, card=None, name=None)
    sysstat._stat_cache.update(t=0.0, val={}, cpu=None)
    yield


PCI_DB = "\n".join(
    [
        "# a comment line",
        "",
        "1002  Advanced Micro Devices, Inc. [AMD/ATI]",
        "\t1234  Plain Device Name",
        "\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX/7900M]",
        "\t\t1da2 5320  PULSE RX 7900 XTX",
        "10de  NVIDIA Corporation",
        "\t2684  AD102 [GeForce RTX 4090]",
    ]
)


@pytest.fixture
def pci_ids(monkeypatch, home):
    """A pci.ids on disk, preceded by one that is not.

    The missing path is deliberate: distributions disagree about where the
    file lives, and skipping a path that is not there is the normal case.
    """
    db = home / "pci.ids"
    db.write_text(PCI_DB + "\n")
    monkeypatch.setattr(sysstat, "PCI_IDS", (str(home / "absent.ids"), str(db)))
    return db


class TestPciName:
    """Turning a device id into the name written on the box."""

    def test_the_subsystem_line_names_the_actual_board(self, pci_ids):
        assert sysstat._pci_name("1002:744c", "1da2", "5320") == "PULSE RX 7900 XTX"

    def test_an_unknown_subsystem_falls_back_to_the_bracketed_model(self, pci_ids):
        # "Navi 31" is the codename; the bracket holds the name a person knows.
        assert sysstat._pci_name("1002:744c", "0000", "0000") == "Radeon RX 7900 XT/7900 XTX/7900M"

    def test_a_device_with_no_bracket_is_used_whole(self, pci_ids):
        assert sysstat._pci_name("1002:1234", "0", "0") == "Plain Device Name"

    def test_a_vendor_that_is_not_listed_keeps_its_numbers(self, pci_ids):
        assert sysstat._pci_name("9999:0001", "0", "0") == "9999:0001"

    def test_something_that_is_not_a_device_id_is_returned_as_it_came(self, pci_ids):
        assert sysstat._pci_name("744c", "0", "0") == "744c"

    def test_no_database_anywhere_keeps_the_numbers(self, monkeypatch, home):
        monkeypatch.setattr(sysstat, "PCI_IDS", (str(home / "nope.ids"),))
        assert sysstat._pci_name("1002:744c", "0", "0") == "1002:744c"


class TestRead:
    """The sysfs one-liner reader every GPU number goes through."""

    def test_it_casts_what_it_finds(self, home):
        (home / "n").write_text(" 42\n")
        assert sysstat._read(home / "n", int) == 42

    def test_a_file_that_is_not_there_is_none(self, home):
        assert sysstat._read(home / "absent") is None

    def test_a_value_that_will_not_cast_is_none(self, home):
        (home / "n").write_text("not a number")
        assert sysstat._read(home / "n", int) is None


def a_card(root, name, vram_total=None, **files):
    dev = root / name / "device"
    dev.mkdir(parents=True)
    if vram_total is not None:
        (dev / "mem_info_vram_total").write_text(str(vram_total))
    for k, v in files.items():
        (dev / k).write_text(v)
    return dev


class TestGpuCard:
    """Which card is worth reporting on a machine that has two."""

    def test_the_card_with_the_most_memory_wins(self, monkeypatch, home):
        # A desktop with a discrete card also has the one in the processor,
        # and reporting the idle integrated 512 MB would be true and useless.
        drm = home / "drm"
        a_card(drm, "card0", vram_total=512 * 1024**2)
        big = a_card(drm, "card1", vram_total=24 * 1024**3)
        monkeypatch.setattr(sysstat, "DRM", drm)
        assert sysstat._gpu_card() == big

    def test_a_card_with_no_vram_file_is_not_a_candidate(self, monkeypatch, home):
        drm = home / "drm"
        a_card(drm, "card0")
        monkeypatch.setattr(sysstat, "DRM", drm)
        assert sysstat._gpu_card() is None

    def test_no_drm_directory_is_no_card(self, monkeypatch, home):
        monkeypatch.setattr(sysstat, "DRM", home / "nothing-here")
        assert sysstat._gpu_card() is None

    def test_a_drm_directory_that_will_not_list_is_no_card(self, monkeypatch):
        def _boom(pat):
            raise OSError("permission denied")

        monkeypatch.setattr(sysstat, "DRM", types.SimpleNamespace(glob=_boom))
        assert sysstat._gpu_card() is None


class TestGpuSysfs:
    """amdgpu publishes everything needed, so nothing has to be shelled out."""

    def test_no_card_at_all_reports_nothing(self, monkeypatch, home):
        monkeypatch.setattr(sysstat, "DRM", home / "nothing-here")
        assert sysstat._gpu_sysfs() is None

    def test_a_card_with_no_total_reports_nothing(self, monkeypatch, home):
        dev = a_card(home / "drm", "card0", vram_total=0)
        sysstat._gpu_cache["card"] = dev
        assert sysstat._gpu_sysfs() is None

    def test_it_reads_the_name_utilisation_and_memory(self, monkeypatch, home, pci_ids):
        dev = a_card(
            home / "drm",
            "card0",
            vram_total=1000,
            mem_info_vram_used="250",
            gpu_busy_percent="37",
            uevent="DRIVER=amdgpu\nPCI_ID=1002:744C\n",
            subsystem_vendor="0x1da2",
            subsystem_device="0x5320",
        )
        sysstat._gpu_cache["card"] = dev
        g = sysstat._gpu_sysfs()
        assert g == {
            "name": "PULSE RX 7900 XTX",
            "busy": 37.0,
            "vram_used": 250.0,
            "vram_total": 1000.0,
        }

    def test_a_card_that_does_not_say_what_it_is_is_just_a_gpu(self, monkeypatch, home):
        dev = a_card(home / "drm", "card0", vram_total=1000, uevent="DRIVER=amdgpu\n")
        sysstat._gpu_cache["card"] = dev
        assert sysstat._gpu_sysfs()["name"] == "GPU"

    def test_an_unreadable_uevent_still_yields_a_card(self, monkeypatch, home):
        dev = a_card(home / "drm", "card0", vram_total=1000)
        sysstat._gpu_cache["card"] = dev
        assert sysstat._gpu_sysfs()["name"] == "GPU"

    def test_the_name_is_looked_up_once(self, monkeypatch, home):
        dev = a_card(home / "drm", "card0", vram_total=1000, uevent="PCI_ID=1002:744C\n")
        sysstat._gpu_cache["card"] = dev
        calls = []
        monkeypatch.setattr(sysstat, "_pci_name", lambda p, sv, sd: calls.append(p) or "A Card")
        sysstat._gpu_sysfs()
        sysstat._gpu_sysfs()
        assert calls == ["1002:744C"]


class TestGpuNvidia:
    """The other half of the world, asked only when sysfs had nothing."""

    def test_without_the_tool_there_is_no_answer(self, monkeypatch):
        monkeypatch.setattr(sysstat.shutil, "which", lambda n: None)
        assert sysstat._gpu_nvidia() is None

    def test_the_csv_line_becomes_bytes_and_percentages(self, monkeypatch):
        monkeypatch.setattr(sysstat.shutil, "which", lambda n: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(
            sysstat.subprocess,
            "run",
            lambda cmd, **kw: types.SimpleNamespace(stdout="GeForce RTX 4090, 31, 2048, 24564\n"),
        )
        g = sysstat._gpu_nvidia()
        assert g["name"] == "GeForce RTX 4090"
        assert g["busy"] == 31.0
        assert g["vram_used"] == 2048 * 1024**2

    def test_a_tool_that_answers_nonsense_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(sysstat.shutil, "which", lambda n: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(
            sysstat.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout="")
        )
        assert sysstat._gpu_nvidia() is None


class TestGpuStats:
    """The cached, front-end-facing answer."""

    def test_sysfs_is_preferred_and_the_percentage_is_added(self, monkeypatch):
        monkeypatch.setattr(
            sysstat,
            "_gpu_sysfs",
            lambda: {"name": "A", "busy": 5.0, "vram_used": 250.0, "vram_total": 1000.0},
        )
        monkeypatch.setattr(sysstat, "_gpu_nvidia", lambda: pytest.fail("asked anyway"))
        assert sysstat.gpu_stats()["vram"] == 25.0

    def test_nvidia_is_the_fallback(self, monkeypatch):
        monkeypatch.setattr(sysstat, "_gpu_sysfs", lambda: None)
        monkeypatch.setattr(
            sysstat,
            "_gpu_nvidia",
            lambda: {"name": "N", "busy": 0.0, "vram_used": 0.0, "vram_total": 0.0},
        )
        # A total of zero is not a card to divide by, so no percentage is added.
        assert "vram" not in sysstat.gpu_stats()

    def test_a_machine_with_no_readable_card_shows_no_rows(self, monkeypatch):
        monkeypatch.setattr(sysstat, "_gpu_sysfs", lambda: None)
        monkeypatch.setattr(sysstat, "_gpu_nvidia", lambda: None)
        assert sysstat.gpu_stats() is None

    def test_a_reader_that_raises_shows_no_rows(self, monkeypatch):
        def _boom():
            raise OSError("sysfs")

        monkeypatch.setattr(sysstat, "_gpu_sysfs", _boom)
        assert sysstat.gpu_stats() is None

    def test_the_answer_is_held_for_a_second_and_a_half(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sysstat, "_gpu_sysfs", lambda: calls.append(1) or None)
        monkeypatch.setattr(sysstat, "_gpu_nvidia", lambda: None)
        sysstat.gpu_stats()
        sysstat.gpu_stats()
        assert len(calls) == 1


# --- the machine ---------------------------------------------------------


def blocking_path(*blocked):
    """A stand-in for pathlib.Path that refuses to construct certain paths."""
    real = pathlib.Path

    class _Path:
        def __new__(cls, *a, **kw):
            p = real(*a, **kw)
            if str(p) in blocked:
                raise OSError(f"{p} is unreadable")
            return p

        home = staticmethod(real.home)

    return _Path


class TestCpuSample:
    """The jiffy pair a CPU percentage is a difference between."""

    def test_it_reads_busy_and_total(self):
        busy, total = sysstat._cpu_sample()
        assert 0 < busy < total

    def test_off_linux_there_is_no_sample(self, monkeypatch):
        monkeypatch.setattr(sysstat, "Path", blocking_path("/proc/stat"))
        assert sysstat._cpu_sample() is None


class TestSystemStats:
    """CPU, memory and disk, out of /proc rather than out of psutil."""

    @pytest.fixture(autouse=True)
    def _no_gpu(self, monkeypatch):
        monkeypatch.setattr(sysstat, "gpu_stats", lambda: None)

    def test_it_answers_every_row_the_hud_draws(self):
        s = sysstat.system_stats()
        assert set(s) >= {"cpu", "mem", "disk", "disk_free", "load", "gpu"}
        assert 0.0 <= s["mem"] <= 100.0

    def test_the_first_reading_is_zero_because_nothing_was_measured_yet(self):
        assert sysstat.system_stats()["cpu"] == 0.0

    def test_the_second_reading_is_the_difference_between_samples(self, monkeypatch):
        sysstat._stat_cache["cpu"] = (100, 200)
        monkeypatch.setattr(sysstat, "_cpu_sample", lambda: (150, 400))
        assert sysstat.system_stats()["cpu"] == 25.0

    def test_a_counter_that_went_backwards_is_reported_as_idle(self, monkeypatch):
        sysstat._stat_cache["cpu"] = (100, 400)
        monkeypatch.setattr(sysstat, "_cpu_sample", lambda: (150, 200))
        assert sysstat.system_stats()["cpu"] == 0.0

    def test_the_answer_is_held_for_a_second_and_a_half(self, monkeypatch):
        first = sysstat.system_stats()
        monkeypatch.setattr(sysstat, "_cpu_sample", lambda: pytest.fail("resampled"))
        assert sysstat.system_stats() is first

    def test_unreadable_memory_is_zero_rather_than_an_empty_window(self, monkeypatch):
        monkeypatch.setattr(sysstat, "Path", blocking_path("/proc/meminfo"))
        s = sysstat.system_stats()
        assert s["mem"] == 0.0 and s["mem_total"] == 0.0

    def test_a_disk_that_will_not_answer_is_zero(self, monkeypatch):
        def _boom(p):
            raise OSError("statvfs")

        monkeypatch.setattr(sysstat.os, "statvfs", _boom)
        assert sysstat.system_stats()["disk"] == 0.0

    def test_a_platform_with_no_load_average_reports_none(self, monkeypatch):
        monkeypatch.delattr(sysstat.os, "getloadavg")
        assert sysstat.system_stats()["load"] == [0, 0, 0]


# --- the voice switch and the reactor ------------------------------------
