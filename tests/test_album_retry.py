import httpx
import pytest
from PIL import Image

from app import downloader as d


@pytest.fixture
def clock(monkeypatch):
    current = [0.0]
    monkeypatch.setattr(d.time, 'monotonic', lambda: current[0])
    monkeypatch.setattr(d.time, 'sleep', lambda delay: current.__setitem__(0, current[0] + delay))
    return current


def test_image_stream_reset_restarts_only_failed_image_in_order(tmp_path, clock):
    calls = []
    def asset(url, output, timeout):
        calls.append((url, timeout))
        if url == 'second' and sum(u == url for u, _ in calls) == 1:
            output.write_bytes(b'partial image')
            raise httpx.ReadError('https://private/?token=secret')
        assert not output.exists()
        Image.new('RGB', (10, 20), 'red').save(output, 'JPEG')
    images, logs = d._download_douyin_images(['first', 'second', 'last'], tmp_path, 10, download_asset=asset)
    assert [u for u, _ in calls] == ['first', 'second', 'second', 'last']
    assert [p.name for p in images] == [f'image-{i:03}.jpg' for i in range(3)]
    assert calls[2][1] == 9.5
    assert 'secret' not in logs


@pytest.mark.parametrize('status,count', [(403, 1), (404, 1), (429, 3), (503, 3)])
def test_status_retries_are_bounded_and_safe(tmp_path, clock, status, count, caplog):
    calls = []
    def asset(url, output, timeout):
        calls.append(url)
        response = httpx.Response(status, request=httpx.Request('GET', 'https://img.max-c.com/a?token=SECRET'))
        response.raise_for_status()
    with pytest.raises(d.DownloadError) as error:
        d._download_douyin_images(['https://img.max-c.com/a'], tmp_path, 10, download_asset=asset)
    assert len(calls) == count
    assert f'HTTP {status}' in str(error.value)
    assert 'SECRET' not in str(error.value) + caplog.text


def test_album_deadline_prevents_retry_and_further_network_calls(tmp_path, clock):
    calls = []
    def asset(url, output, timeout):
        calls.append(timeout)
        clock[0] += timeout
        raise httpx.ReadTimeout('expired')
    with pytest.raises(d.DownloadError):
        d._download_douyin_images(['first', 'second'], tmp_path, 10, download_asset=asset)
    assert calls == [10]


def test_local_invalid_image_is_not_retried(tmp_path, clock):
    calls = []
    def asset(url, output, timeout):
        calls.append(url)
        output.write_bytes(b'not an image')
    with pytest.raises(d.DownloadError):
        d._download_douyin_images(['bad'], tmp_path, 10, download_asset=asset)
    assert calls == ['bad']
