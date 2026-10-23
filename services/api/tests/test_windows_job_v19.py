"""Windows Job Object 进执行层后的真值验收（M1a）。

这里每条断言都是「同一个命令、只改一处配置，结果必须不同」的形状：
只断言"代码调用了 CreateJobObject"不算证据——面板上写 512m 而内核根本没拦，才是本立项要消灭的谎。
非 Windows 平台整组跳过（Job Object 是 Win32 原语，不是我们可以"降级测试"的东西）。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from blank_api.services.sandbox import windows_job
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.sandbox.runner import LocalSandbox, _enforcement_notes

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 是 Windows 原语")

PY = Path(sys.executable)


def _policy(**overrides) -> SandboxPolicy:
    policy = SandboxPolicy()
    policy.timeout_seconds = 60
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def _py(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _cmd(path: Path, *args: str) -> str:
    return " ".join([f'"{PY}"', f'"{path}"', *args])


async def _run(tmp_path: Path, script: str, **overrides):
    return await LocalSandbox(_policy(**overrides)).run(script, workspace=str(tmp_path))


def test_parse_size_only_understands_real_sizes() -> None:
    assert windows_job.parse_size("512m") == 512 * 1024 * 1024
    assert windows_job.parse_size("128k") == 128 * 1024
    assert windows_job.parse_size("1g") == 1024 ** 3
    assert windows_job.parse_size(2048) == 2048
    assert windows_job.parse_size("0") == 0
    assert windows_job.parse_size(-5) == 0
    # 认不出来必须是 0（= 这条不设限），不许悄悄回落到某个默认值再声称"限额已生效"
    assert windows_job.parse_size("随便写点什么") == 0
    assert windows_job.parse_size("") == 0


async def test_memory_limit_denies_and_reports_itself(tmp_path: Path) -> None:
    """P-MEM：48m 上限真的拦住 256MB 分配，且这条命令的账上写着"由 Job 强制"。"""
    alloc = _py(tmp_path, "alloc.py",
                "import sys\nmb=int(sys.argv[1])\n"
                "buf=bytearray(mb*1024*1024);buf[0]=1\nprint('ALLOC_OK',len(buf))\n")
    tight = await _run(tmp_path, _cmd(alloc, "256"), memory="48m")
    loose = await _run(tmp_path, _cmd(alloc, "256"), memory="512m")
    assert "ALLOC_OK" in loose.output(), (
        f"对照失败：512m 下这条命令自己都跑不通，后面的 DENIED 没有意义：{loose.output()[-160:]}")
    assert "ALLOC_OK" not in tight.output() and tight.exit_code != 0, (
        f"内存上限没被内核强制：{tight.output()[-200:]}")
    # 回归守卫：enforcement 必须在 close_job 之前算。句柄一置 0，"被内核限着 48MB"
    # 就会记成"限额未强制"——反向说谎同样是谎，这一条专门盯着它。
    assert tight.enforcement["job_created"] is True, tight.enforcement
    assert tight.enforcement["job_assigned"] is True
    assert tight.enforcement["memory_enforced"] is True
    assert tight.enforcement["memory_bytes"] == 48 * 1024 * 1024
    assert loose.enforcement["memory_enforced"] is True


async def test_pids_limit_refuses_second_process_in_tree(tmp_path: Path) -> None:
    """P-PIDS：policy.pids=1 时，命令树里的第二个进程创建不出来（内核直接拒，不是我们数出来的）。

    顺带记下这条对运维有用的事实：计数包含 cmd 外壳自己，所以 pids=1 连"跑一个 python 脚本"
    都做不到。面板上的默认值（128）必须留出外壳+解释器+子进程的余量。
    """
    spawn = _py(tmp_path, "spawn.py",
                "import subprocess,sys,time\n"
                "time.sleep(1.2)\n"
                "r=subprocess.run([sys.executable,'-c','print(1)'],capture_output=True)\n"
                "print('CHILD_RC',r.returncode)\n")
    free = await _run(tmp_path, _cmd(spawn), memory="512m", pids=0)
    limited = await _run(tmp_path, _cmd(spawn), memory="512m", pids=1)
    assert "CHILD_RC 0" in free.output(), f"对照失败：{free.output()[-200:]}"
    assert free.enforcement["pids_enforced"] is False and free.enforcement["pids_limit"] == 0, (
        "不设限的时候必须写「没强制」，不许把 0 说成 128")
    assert "CHILD_RC 0" not in limited.output(), (
        f"pids=1 没拦住第二个进程：{limited.output()[-200:]}")
    assert limited.enforcement["pids_enforced"] is True and limited.enforcement["pids_limit"] == 1


async def test_closing_the_job_takes_the_whole_tree(tmp_path: Path) -> None:
    """P-TREE：命令结束后脱离父进程的孙进程必须死。对照组是不挂 Job 的同一条命令。

    这条测的是"停止任务之后不存在残留进程"那句承诺。旧实现只在超时时 taskkill，
    父进程正常退出、孙子挂在后台的场景一个都不管。
    """
    sleeper = _py(tmp_path, "sleeper.py",
                  "import sys,time\n"
                  "open(sys.argv[1],'w').write('STARTED')\n"
                  "time.sleep(float(sys.argv[2]))\n"
                  "open(sys.argv[1],'w').write('ALIVE')\n")
    spawner = _py(tmp_path, "spawner.py",
                  "import subprocess,sys,time,pathlib\n"
                  "out=pathlib.Path(sys.argv[2])\n"
                  "subprocess.Popen([sys.executable,sys.argv[1],str(out),'2.5'],\n"
                  "    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,\n"
                  "    stdin=subprocess.DEVNULL,creationflags=0x00000008)\n"
                  "end=time.time()+8\n"
                  "while time.time()<end and not out.exists():\n"
                  "    time.sleep(0.05)\n"
                  "print('STARTED_SEEN',out.exists())\n")
    under = tmp_path / "under-job.txt"
    control = tmp_path / "no-job.txt"
    sandboxed = await _run(tmp_path, _cmd(spawner, f'"{sleeper}"', f'"{under}"'), memory="512m")
    proc = await asyncio.create_subprocess_shell(
        _cmd(spawner, f'"{sleeper}"', f'"{control}"'), cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, creationflags=0x00000200)
    free_out, _ = await asyncio.wait_for(proc.communicate(), 30)
    assert sandboxed.exit_code == 0 and not sandboxed.timed_out, (
        "前置失败：父命令自己没正常结束，就分不清是被 Job 带走还是被我们 kill 的")
    assert "STARTED_SEEN True" in sandboxed.stdout, "孙进程压根没起来，后面的结论全是空的"
    assert sandboxed.enforcement["tree_kill_enforced"] is True
    await asyncio.sleep(4.0)
    assert under.read_text(encoding="utf-8") == "STARTED", (
        f"Job 关了孙进程还活着（残留进程 = {under.read_text(encoding='utf-8')}）")
    assert control.read_text(encoding="utf-8") == "ALIVE", (
        f"对照失败：不挂 Job 时它也死了，装置本身不可信（{free_out!r}）")


async def test_host_console_gbk_stays_readable(tmp_path: Path) -> None:
    """宿主子进程写 GBK 时不许解成一堆替换字符。

    不是舒适度问题：中文 Windows 上「配额不足，无法处理此命令。」正是限额被内核拒绝的原文，
    一律按 UTF-8 硬解就等于把最关键的证据丢掉。
    """
    emit = _py(tmp_path, "gbk.py",
               "import sys\n"
               "sys.stdout.buffer.write('配额不足，无法处理此命令。\\n'.encode('gbk'))\n")
    res = await _run(tmp_path, _cmd(emit), memory="512m")
    assert "配额不足，无法处理此命令。" in res.stdout, repr(res.stdout)
    assert "\ufffd" not in res.stdout


def test_notes_state_what_is_not_enforced() -> None:
    """notes 是给人读的，必须和 enforcement 同源；未强制的项要逐条说清，不许写"已隔离"。"""
    job = windows_job.create_job("blank-test-notes", memory_bytes=64 * 1024 * 1024)
    try:
        facts = {
            "boundary_strength": "job_only", "boundary_strength_label": "仅资源与生命周期强制",
            "boundary_measured_seconds_ago": 1.0, "boundary_apparatus_ok": True,
            "job_created": job.ok, "job_assigned": True, "job_error": job.error,
            "memory_enforced": True, "memory_bytes": 64 * 1024 * 1024,
            "pids_enforced": True, "pids_limit": 128, "tree_kill_enforced": True,
            "filesystem_enforced": False, "network_enforced": False,
            "cpu_enforced": False, "disk_enforced": False,
        }
    finally:
        windows_job.close_job(job)
    assert job.ok is False, "close_job 之后句柄必须归零：enforcement 只能在建 Job 当场算"
    notes = " ".join(_enforcement_notes(facts))
    assert "Job Object 强制" in notes and "64.0MB" in notes
    assert "文件边界未由系统强制" in notes and "网络边界未由系统强制" in notes
    assert "已隔离" not in notes
