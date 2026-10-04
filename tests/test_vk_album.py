"""Tests for the bundled photo album (``send_multiple_images``).

Measured live 04.10.2026 (why the cap and the deferral exist): VK does NOT error on too many
attachments — a `messages.send` carrying 12 photos is accepted and only **10** arrive. So an override
that simply joins everything would silently drop the tail; the cap plus the base-path hand-off is the
part that keeps every image reachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

import _paths  # noqa: E402  (registers the plugin as `vk`)

for _inherited in [name for name in list(os.environ) if name.startswith("VK_")]:
    os.environ.pop(_inherited, None)

from vk.adapter import MAX_ALBUM_ATTACHMENTS, VKAdapter  # noqa: E402


def make_adapter() -> VKAdapter:
    from test_vk_adapter import FakeClient
    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry

    if not platform_registry.is_registered("vk"):
        Platform._add_pseudo_member("vk")
    adapter = VKAdapter(PlatformConfig(extra={}))
    adapter.client = FakeClient()

    async def numbered_upload(data, filename="image.jpg"):
        adapter.client.photo_uploads.append((filename, len(data)))
        return f"photo-777_{len(adapter.client.photo_uploads)}"

    adapter.client.upload_photo = numbered_upload
    return adapter


def image_files(tmp_path: pathlib.Path, count: int) -> list:
    out = []
    for i in range(count):
        path = tmp_path / f"pic{i}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(32))
        out.append((str(path), f"подпись {i}"))
    return out


def album(adapter, images, **kwargs):
    return asyncio.run(adapter.send_multiple_images("123456", images, **kwargs))


# ── the bundle ──────────────────────────────────────────────────────────────

def test_photos_ride_one_message_with_the_captions_joined(tmp_path):
    adapter = make_adapter()
    result = album(adapter, image_files(tmp_path, 3))
    assert result.success is True
    assert len(adapter.client.sent) == 1, "an album must be ONE message, not one per photo"
    sent = adapter.client.sent[0]
    assert sent["attachment"] == "photo-777_1,photo-777_2,photo-777_3"
    assert sent["message"] == "подпись 0 · подпись 1 · подпись 2"
    assert sent["peer_id"] == 123456


def test_an_empty_caption_is_allowed(tmp_path):
    adapter = make_adapter()
    assert album(adapter, [(str(p), "") for p, _ in image_files(tmp_path, 2)]).success is True
    assert adapter.client.sent[0]["message"] == ""
    assert adapter.client.sent[0]["attachment"] == "photo-777_1,photo-777_2"


def test_the_caption_is_capped_at_one_vk_message(tmp_path):
    adapter = make_adapter()
    long_alts = [(str(tmp_path / "p.png"), "х" * 3000), (str(tmp_path / "q.png"), "у" * 3000)]
    for path, _ in long_alts:
        pathlib.Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(16))
    assert album(adapter, long_alts).success is True
    assert len(adapter.client.sent[0]["message"]) <= adapter.MAX_MESSAGE_LENGTH


def test_url_images_are_fetched_and_uploaded(tmp_path):
    adapter = make_adapter()
    local = tmp_path / "local.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(16))
    result = album(adapter, [("https://cdn.example/one.png", "alt"), (str(local), "")])
    assert result.success is True
    assert len(adapter.client.sent) == 1
    assert adapter.client.sent[0]["attachment"].count(",") == 1


# ── the ceiling and the hand-off ────────────────────────────────────────────

def test_more_than_the_cap_goes_out_in_a_second_message(tmp_path):
    adapter = make_adapter()
    images = image_files(tmp_path, MAX_ALBUM_ATTACHMENTS + 2)
    result = album(adapter, images)
    assert result.success is True
    assert len(adapter.client.sent) == 1 + 2, "the tail must be delivered, not silently truncated"
    assert adapter.client.sent[0]["attachment"].count(",") == MAX_ALBUM_ATTACHMENTS - 1
    assert [call["attachment"] for call in adapter.client.sent[1:]] == ["photo-777_11", "photo-777_12"]


def test_a_gif_is_left_to_the_base_path(tmp_path):
    adapter = make_adapter()
    assert adapter._is_bundleable_photo("https://cdn.example/anim.gif") is False
    assert adapter._is_bundleable_photo("https://cdn.example/photo.jpg") is True
    assert adapter._is_bundleable_photo("/tmp/notes.txt") is False
    result = album(adapter, [("https://cdn.example/anim.gif", "gif"), ("https://cdn.example/photo.jpg", "фото")])
    assert result.success is True
    album_calls = [call for call in adapter.client.sent if call.get("attachment") == "photo-777_1"]
    assert album_calls, "the static photo still rides as a photo attachment"


def test_a_failed_upload_defers_that_image_instead_of_dropping_it(tmp_path):
    adapter = make_adapter()
    real = adapter.client.upload_photo
    calls = {"n": 0}

    async def flaky(data, filename="image.jpg"):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("upload refused")
        return await real(data, filename)

    adapter.client.upload_photo = flaky
    images = image_files(tmp_path, 3)
    result = album(adapter, images)
    assert result.success is True
    # the album carried the two that uploaded; the refused one went back through the base path
    assert adapter.client.sent[0]["attachment"] == "photo-777_1,photo-777_2"
    assert len(adapter.client.sent) == 2, "the refused image must be retried as its own message"
    assert adapter.client.sent[1].get("attachment"), "the retry must carry the photo, not the path"
    assert len(adapter.client.photo_uploads) == 3, "the retry re-uploads the image it lost"


def test_an_album_send_failure_falls_back_to_one_message_per_photo(tmp_path):
    adapter = make_adapter()

    async def boom(peer_id, message, **kwargs):
        if kwargs.get("attachment", "").count(",") >= 1:
            raise RuntimeError("messages.send refused")
        adapter.client.sent.append({"peer_id": peer_id, "message": message, **kwargs})
        return 900 + len(adapter.client.sent)

    adapter.client.send_message = boom
    result = album(adapter, image_files(tmp_path, 2))
    assert result.success is True, "uploads were already paid for — the photos must still arrive"
    assert [call["attachment"] for call in adapter.client.sent] == ["photo-777_1", "photo-777_2"]


# ── refusals ────────────────────────────────────────────────────────────────

def test_every_upload_failing_is_reported_as_failure(tmp_path):
    adapter = make_adapter()

    async def boom(data, filename="image.jpg"):
        raise RuntimeError("no upload server")

    adapter.client.upload_photo = boom
    result = album(adapter, image_files(tmp_path, 2))
    assert result.success is False and "no upload server" in (result.error or "")


def test_empty_input_and_bad_peer_are_refused_without_sending():
    adapter = make_adapter()
    assert album(adapter, []).success is False
    assert asyncio.run(adapter.send_multiple_images("не-число", [("https://x/y.png", "a")])).success is False
    assert adapter.client.sent == []


def test_missing_client_is_refused():
    adapter = make_adapter()
    adapter.client = None
    assert asyncio.run(adapter.send_multiple_images("123456", [("https://x/y.png", "a")])).success is False


def test_the_override_satisfies_the_base_contract():
    """The base documents success = at least one image delivered; the turn tracker depends on it."""
    assert callable(getattr(VKAdapter, "send_multiple_images", None))
    assert VKAdapter.send_multiple_images is not __import__("gateway.platforms.base", fromlist=["x"]).BasePlatformAdapter.send_multiple_images


if __name__ == "__main__":
    print("запускается через pytest; здесь только проверка импорта")
