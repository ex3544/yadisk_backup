"""Тесты политики повторов: что ретраим, что нет и сколько ждём."""

import yadisk

from downloader import _is_retryable, _wait_policy, _download_one, DownloadJob


class _Outcome:
    def __init__(self, exc):
        self._exc = exc

    def exception(self):
        return self._exc


class _RetryState:
    """Минимальный дублёр tenacity.RetryCallState."""

    def __init__(self, exc, attempt=1):
        self.outcome = _Outcome(exc)
        self.attempt_number = attempt
        self.idle_for = 0
        self.seconds_since_start = 0


def _err(cls):
    """Создаёт исключение yadisk без реального HTTP-ответа."""
    return cls("boom")


def test_unauthorized_is_not_retried():
    """Битый токен не станет валидным — не тратим 5 попыток на каждый файл."""
    assert _is_retryable(_err(yadisk.exceptions.UnauthorizedError)) is False


def test_forbidden_is_not_retried():
    assert _is_retryable(_err(yadisk.exceptions.ForbiddenError)) is False


def test_path_not_found_is_not_retried():
    assert _is_retryable(_err(yadisk.exceptions.PathNotFoundError)) is False


def test_rate_limit_is_retried():
    assert _is_retryable(_err(yadisk.exceptions.TooManyRequestsError)) is True


def test_network_errors_are_retried():
    assert _is_retryable(ConnectionError("reset")) is True
    assert _is_retryable(TimeoutError("slow")) is True
    assert _is_retryable(OSError("io")) is True


def test_unknown_exception_is_not_retried():
    assert _is_retryable(ValueError("nonsense")) is False


def test_wait_policy_sleeps_a_minute_on_429():
    """Раньше эта ветка была мёртвым кодом и не срабатывала никогда."""
    wait = _wait_policy(_RetryState(_err(yadisk.exceptions.TooManyRequestsError)))
    assert wait == 60.0


def test_wait_policy_is_exponential_otherwise():
    w1 = _wait_policy(_RetryState(ConnectionError("x"), attempt=1))
    w3 = _wait_policy(_RetryState(ConnectionError("x"), attempt=3))
    assert w1 < w3 <= 60


# ── resume-логика ───────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, payload=b"data", fail_with=None):
        self.payload = payload
        self.fail_with = fail_with
        self.calls = []

    def download(self, remote, local):
        self.calls.append(remote)
        if self.fail_with:
            raise self.fail_with
        with open(local, "wb") as f:
            f.write(self.payload)


def test_existing_file_with_same_size_is_skipped(tmp_path):
    target = tmp_path / "disk" / "photo.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"1234")

    cli = FakeClient()
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/photo.jpg", size=4, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "skipped"
    assert cli.calls == []  # сеть не трогали


def test_partial_file_is_redownloaded(tmp_path):
    target = tmp_path / "disk" / "photo.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"12")  # обрыв на середине

    cli = FakeClient(payload=b"1234")
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/photo.jpg", size=4, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "downloaded"
    assert res.bytes_written == 4
    assert target.read_bytes() == b"1234"


def test_failure_is_reported_not_raised(tmp_path):
    cli = FakeClient(fail_with=yadisk.exceptions.PathNotFoundError("gone"))
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/gone.jpg", size=10, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "failed"
    assert "PathNotFound" in (res.error or "")


def test_missing_folder_is_failed_not_raised(tmp_path):
    """Отключённый диск посреди прохода: файл сбойный, потоки живы.

    В 35.5.7 здесь вылетал FileNotFoundError [WinError 3] и приложение
    показывало окно «Unhandled exception in script».
    """
    blocker = tmp_path / "disk.txt"
    blocker.write_text("x")  # файл на месте будущей папки — makedirs не сможет
    cli = FakeClient(payload=b"1234")
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/photo.jpg", size=4, md5=None),
        cli, str(blocker / "sub"),
    )
    assert res.status == "failed"
    assert "Не удалось создать папку" in (res.error or "")
    assert cli.calls == []


def test_download_all_refuses_unavailable_target_dir(tmp_path):
    """Перед стартом потоков папка проверяется целиком, а не по файлу."""
    import pytest

    import downloader

    blocker = tmp_path / "disk.txt"
    blocker.write_text("x")
    with pytest.raises(SystemExit) as exc:
        downloader.download_all("token", state=None, download_dir=str(blocker / "sub"))
    assert exc.value.code == 2


def test_empty_pass_keeps_session_totals(tmp_path, monkeypatch):
    """Второй проход без файлов не обнуляет итог сессии.

    Регресс: при `download --retry` пустая очередь перезаписывала файл статуса
    нулями, и плашка показывала только последний проход.
    """
    import json

    import downloader

    status = tmp_path / "status.json"
    monkeypatch.setattr(downloader, "DISK_STATUS_FILE", status)

    total = {"downloaded": 58, "skipped": 3, "failed": 451, "bytes": 1024}
    out = downloader.download_all("token", state=None, download_dir=str(tmp_path),
                                  jobs=iter([]), session=dict(total))

    assert out == {**total, "deferred": 0}
    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["downloaded"] == 58
    assert data["skipped"] == 3
    assert data["failed"] == 451
    assert data["active"] is False
    assert data["started_at"] == downloader.SESSION_STARTED_AT


def test_start_status_is_zero_and_active(tmp_path, monkeypatch):
    """Ранняя запись: плашка сразу «идёт», а не «подготовка»."""
    import json

    import downloader

    status = tmp_path / "status.json"
    monkeypatch.setattr(downloader, "DISK_STATUS_FILE", status)

    downloader.write_status_start()

    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["active"] is True
    assert data["downloaded"] == 0
    assert data["updated_at"] >= downloader.SESSION_STARTED_AT


def test_download_stops_on_insufficient_disk_space(tmp_path, monkeypatch):
    import downloader
    # Эмулируем нехватку свободного места
    monkeypatch.setattr(downloader, "check_free_space", lambda path, req: False)
    cli = FakeClient()
    res = downloader._download_one(
        DownloadJob(remote_path="disk:/huge.iso", size=100 * 1024 * 1024, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "disk_full"
    assert "NoSpaceLeft" in (res.error or "")
    assert cli.calls == []  # скачивание даже не начиналось


# ── отложенные файлы и предохранитель B0 (35.6.3) ──────────────────────────


class _LinkClient:
    """Клиент с прямой ссылкой: транспорт downloader'а идёт в сеть сам."""

    def __init__(self, link="https://storage.yandex.net/f"):
        self.link = link
        self.calls = []

    def get_download_link(self, remote):
        self.calls.append(remote)
        return self.link


class _Resp:
    def __init__(self, status=200, body=b"", ctype="application/octet-stream"):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": ctype}

    def iter_content(self, chunk_size=1024):
        yield self._body

    def close(self):
        pass


class _HttpSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.resp

    def close(self):
        pass


def test_storage_5xx_marks_file_deferred_not_failed(tmp_path):
    """Молчащий сервер хранения — это «ждёт Яндекса», а не сбой файла."""
    from fetch import DeadHosts

    dead = DeadHosts()
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/a.bin", size=4, md5=None),
        _LinkClient("https://storage.yandex.net/f"),
        str(tmp_path),
        dead_hosts=dead,
        http_session=_HttpSession(_Resp(503)),
    )
    assert res.status == "deferred"
    assert res.kind == "server_50x"
    assert res.host == "storage.yandex.net"
    assert res.is_local_error is False
    # Сервер запомнили мёртвым: следующий файл с него не будет ждать.
    assert dead.is_dead("storage.yandex.net")


def test_deferred_file_is_not_cleaned_and_no_leftover(tmp_path):
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/a.bin", size=4, md5=None),
        _LinkClient(),
        str(tmp_path),
        http_session=_HttpSession(_Resp(500)),
    )
    assert res.status == "deferred"
    assert [p.name for p in (tmp_path / "disk").iterdir()] == []


def test_dead_host_is_skipped_without_network(tmp_path):
    from fetch import DeadHosts

    dead = DeadHosts()
    dead.mark("storage.yandex.net")
    http = _HttpSession(_Resp(200, body=b"1234"))
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/a.bin", size=4, md5=None),
        _LinkClient(),
        str(tmp_path),
        dead_hosts=dead,
        http_session=http,
    )
    assert res.status == "deferred"
    assert res.kind == "storage_skip"
    assert http.calls == []  # в сеть не ходили


def test_ok_transfer_through_own_transport(tmp_path):
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/a.bin", size=4, md5=None),
        _LinkClient(),
        str(tmp_path),
        http_session=_HttpSession(_Resp(200, body=b"1234")),
    )
    assert res.status == "downloaded"
    assert res.bytes_written == 4
    assert (tmp_path / "disk" / "a.bin").read_bytes() == b"1234"


def test_status_code_404_is_failed_not_deferred(tmp_path):
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/a.bin", size=4, md5=None),
        _LinkClient(),
        str(tmp_path),
        http_session=_HttpSession(_Resp(404)),
    )
    assert res.status == "failed"
    assert res.kind == "not_found"


def _failed_local(job):
    from downloader import DownloadResult

    return DownloadResult(
        job=job, status="failed", local_path=None, bytes_written=0,
        error="WinError 3", duration_ms=1, kind="local_disk",
    )


def test_disk_offline_stops_pass_after_limit(tmp_path, monkeypatch):
    """Восемь локальных ошибок подряд — останавливаем проход, а не жжём попытки."""
    import json

    import downloader
    from state import State

    status = tmp_path / "status.json"
    monkeypatch.setattr(downloader, "DISK_STATUS_FILE", status)
    monkeypatch.setattr(downloader, "_download_one", lambda job, *a, **k: _failed_local(job))
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: None)
    monkeypatch.setattr(downloader, "_get_thread_http_session", lambda: None)
    monkeypatch.setattr(downloader, "_close_all_clients", lambda: None)

    st = State(str(tmp_path / "state.db"))
    jobs = [DownloadJob(remote_path=f"disk:/f{i}.bin", size=1, md5=None) for i in range(30)]
    try:
        out = downloader.download_all(
            "token", state=st, download_dir=str(tmp_path), jobs=iter(jobs), workers=1
        )
    finally:
        st.close()

    # Обработали только до порога, остальные остались в очереди.
    assert out["failed"] == downloader.LOCAL_ERROR_LIMIT
    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["state"] == "disk_offline"


def test_successful_file_resets_local_error_streak(tmp_path, monkeypatch):
    """Одиночные локальные ошибки между успехами проход не останавливают."""
    import downloader
    from downloader import DownloadResult
    from state import State

    monkeypatch.setattr(downloader, "DISK_STATUS_FILE", tmp_path / "status.json")
    seq = iter(["fail", "ok"] * 10)

    def fake(job, *a, **k):
        if next(seq) == "fail":
            return _failed_local(job)
        return DownloadResult(job, "skipped", str(tmp_path / "x"), 0, None, 1)

    monkeypatch.setattr(downloader, "_download_one", fake)
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: None)
    monkeypatch.setattr(downloader, "_get_thread_http_session", lambda: None)
    monkeypatch.setattr(downloader, "_close_all_clients", lambda: None)

    st = State(str(tmp_path / "state.db"))
    jobs = [DownloadJob(remote_path=f"disk:/f{i}.bin", size=1, md5=None) for i in range(20)]
    try:
        out = downloader.download_all(
            "token", state=st, download_dir=str(tmp_path), jobs=iter(jobs), workers=1
        )
    finally:
        st.close()
    assert out["failed"] == 10
    assert out["skipped"] == 10


def test_waiting_counter_is_not_doubled(tmp_path, monkeypatch):
    """«Ещё ждут» — сколько файлов ждёт Яндекса сейчас, а не сумма двух подсчётов.

    В 35.6.4 файл, отложенный ещё в прошлый раз, прибавлялся к счётчику из базы
    второй раз: 400 файлов превращались в «ещё ждут: 800».
    """
    import json

    import downloader
    from downloader import DownloadResult
    from state import State

    status = tmp_path / "status.json"
    monkeypatch.setattr(downloader, "DISK_STATUS_FILE", status)
    monkeypatch.setattr(downloader, "_waiting_stats", lambda state: 2)
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: None)
    monkeypatch.setattr(downloader, "_get_thread_http_session", lambda: None)
    monkeypatch.setattr(downloader, "_close_all_clients", lambda: None)

    outcomes = {
        # ждал раньше и ждёт снова — цифра не меняется
        "disk:/a.bin": DownloadResult(None, "deferred", None, 0, "storage_down | h", 1),
        # ждал раньше, но скачался — минус один
        "disk:/b.bin": DownloadResult(None, "downloaded", str(tmp_path / "b.bin"), 5, None, 1),
        # новый файл, Яндекс не отдал — плюс один
        "disk:/c.bin": DownloadResult(None, "deferred", None, 0, "storage_down | h", 1),
    }

    def fake(job, *a, **k):
        return DownloadResult(
            job, outcomes[job.remote_path].status, outcomes[job.remote_path].local_path,
            outcomes[job.remote_path].bytes_written, outcomes[job.remote_path].error, 1,
        )

    monkeypatch.setattr(downloader, "_download_one", fake)

    st = State(str(tmp_path / "state.db"))
    jobs = [
        DownloadJob(remote_path="disk:/a.bin", size=1, md5=None, error="storage_down | h"),
        DownloadJob(remote_path="disk:/b.bin", size=1, md5=None, error="storage_down | h"),
        DownloadJob(remote_path="disk:/c.bin", size=1, md5=None),
    ]
    try:
        downloader.download_all(
            "token", state=st, download_dir=str(tmp_path), jobs=iter(jobs), workers=1
        )
    finally:
        st.close()

    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["waiting"] == 2
