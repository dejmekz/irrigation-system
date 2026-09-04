"""OTA manifest handling. The manifest must state the version that is actually
in the uploaded image, not a count of how many times upload has been called."""
import io
import json
import os

import pytest

from app.firmware import version_from_image


def image(version=None, size=2048):
    """A stand-in for a built image: padding with the marker embedded, or
    without it for an image built before the marker existed."""
    blob = bytearray(b'\xe9' + os.urandom(size))
    if version is not None:
        blob[600:600] = b'IRRIGATION_FW_VERSION=%d:END' % version
    return bytes(blob)


@pytest.fixture
def fw_client(client, tmp_path):
    client.application.config['FW_CFG']['dir'] = str(tmp_path)
    client.fw_dir = tmp_path
    return client


def upload(fw_client, blob):
    return fw_client.post('/firmware/upload', data={
        'firmware': (io.BytesIO(blob), 'firmware.bin')},
        content_type='multipart/form-data')


# --- reading the version out of an image ---

@pytest.mark.parametrize('version', [1, 12, 13, 999])
def test_version_is_read_from_the_image(tmp_path, version):
    path = tmp_path / 'fw.bin'
    path.write_bytes(image(version))
    assert version_from_image(str(path)) == version


def test_image_without_the_marker_returns_none(tmp_path):
    path = tmp_path / 'fw.bin'
    path.write_bytes(image(None))
    assert version_from_image(str(path)) is None


@pytest.mark.parametrize('tail', [b'IRRIGATION_FW_VERSION=:END',
                                  b'IRRIGATION_FW_VERSION=abc:END',
                                  b'IRRIGATION_FW_VERSION=0:END',
                                  b'IRRIGATION_FW_VERSION=12',
                                  b'IRRIGATION_FW_VERSION=99999999999999:END'])
def test_malformed_markers_are_rejected(tmp_path, tail):
    path = tmp_path / 'fw.bin'
    path.write_bytes(b'\xe9' + b'\x00' * 100 + tail + b'\x00' * 100)
    assert version_from_image(str(path)) is None


def test_missing_file_returns_none(tmp_path):
    assert version_from_image(str(tmp_path / 'nope.bin')) is None


# --- what the manifest ends up saying ---

def test_manifest_takes_the_version_from_the_image(fw_client):
    fw_client.mqtt.state = {'fw': 12}
    body = upload(fw_client, image(13)).get_json()
    assert body['version'] == 13
    assert body['version_source'] == 'image'
    manifest = json.loads((fw_client.fw_dir / 'manifest.json').read_text())
    assert manifest['version'] == 13


def test_reuploading_the_same_version_does_not_inflate_the_manifest(fw_client):
    """The old counter incremented on every upload, so the manifest ran ahead of
    the firmware and a trigger re-flashed an image the device already had."""
    fw_client.mqtt.state = {'fw': 12}
    for _ in range(3):
        body = upload(fw_client, image(13)).get_json()
        assert body['version'] == 13


def test_manifest_can_go_back_down_to_match_a_rebuilt_older_image(fw_client):
    fw_client.mqtt.state = {'fw': 12}
    assert upload(fw_client, image(20)).get_json()['version'] == 20
    assert upload(fw_client, image(13)).get_json()['version'] == 13


def test_upload_reports_whether_a_trigger_would_flash(fw_client):
    fw_client.mqtt.state = {'fw': 12}
    assert upload(fw_client, image(13)).get_json()['will_update'] is True
    same = upload(fw_client, image(12)).get_json()
    assert same['will_update'] is False
    assert same['device_version'] == 12
    assert upload(fw_client, image(11)).get_json()['will_update'] is False


def test_unmarked_image_falls_back_to_the_counter(fw_client):
    """Older images carry no marker; they must still produce a flashable
    manifest rather than failing the upload."""
    fw_client.mqtt.state = {'fw': 12}
    body = upload(fw_client, image(None)).get_json()
    assert body['version_source'] == 'counter'
    assert body['version'] == 13          # floored at the device, then +1
    assert body['will_update'] is True


def test_upload_rewrites_host_and_port_from_config(fw_client):
    fw_client.mqtt.state = {'fw': 12}
    (fw_client.fw_dir / 'manifest.json').write_text(json.dumps(
        {'type': 'irrigation-esp32c3', 'version': 1, 'host': 'stale', 'port': 1234}))
    body = upload(fw_client, image(13)).get_json()
    assert body['host'] == 'raspi4server.local'
    assert body['port'] == 80


def test_image_is_written_atomically_and_completely(fw_client):
    blob = image(13, size=5000)
    upload(fw_client, blob)
    assert (fw_client.fw_dir / 'irrigation.bin').read_bytes() == blob
    assert not (fw_client.fw_dir / 'irrigation.bin.part').exists()


def test_upload_without_a_file_is_rejected(fw_client):
    assert fw_client.post('/firmware/upload', data={}).status_code == 400
