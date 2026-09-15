from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

from .models import ActionType, Locator
from .redaction import redact_text


class SurfaceError(RuntimeError):
    """A live surface could not perform or observe an operation."""


class SurfaceTimeout(SurfaceError):
    """The surface did not reach the requested state before its deadline."""


@dataclass(frozen=True)
class Control:
    ephemeral_id: str
    kind: str
    name: str
    locator: Locator

    def to_dict(self) -> dict[str, str | dict[str, Any]]:
        return {
            "id": self.ephemeral_id,
            "kind": self.kind,
            "name": self.name,
            "locator": self.locator.to_dict(),
        }


@dataclass(frozen=True)
class ReadableTarget:
    ephemeral_id: str
    text: str
    locator: Locator

    def to_dict(self) -> dict[str, str | dict[str, Any]]:
        return {
            "id": self.ephemeral_id,
            "text": self.text,
            "locator": self.locator.to_dict(),
        }


@dataclass(frozen=True)
class SurfaceObservation:
    url: str
    title: str
    text: str
    controls: tuple[Control, ...]
    readable_targets: tuple[ReadableTarget, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "controls": [control.to_dict() for control in self.controls],
            "readable_targets": [target.to_dict() for target in self.readable_targets],
        }


class BrowserSurface:
    """Playwright-backed surface using semantic locators before CSS fallbacks."""

    kind = "browser"

    def __init__(self, playwright: Any, browser: Any, context: Any, page: Any):
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self.page = page

    @classmethod
    def open(cls, url: str, *, headless: bool = True) -> "BrowserSurface":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SurfaceError(
                "Playwright is required. Install with: pip install -e '.[browser]' "
                "and run: playwright install chromium"
            ) from exc
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            return cls(playwright, browser, context, page)
        except Exception:
            playwright.stop()
            raise

    @property
    def url(self) -> str:
        return str(self.page.url)

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def observe(self) -> SurfaceObservation:
        try:
            text = str(self.page.locator("body").inner_text())
            title = str(self.page.title())
            controls = tuple(self._controls())
            readable_targets = tuple(self._readable_targets())
            return SurfaceObservation(self.url, title, text, controls, readable_targets)
        except Exception as exc:
            raise SurfaceError(f"observe failed: {exc}") from exc

    def _controls(self) -> list[Control]:
        handles = self.page.locator("a,button,input,select,textarea,[role]")
        result: list[Control] = []
        for index in range(handles.count()):
            element = handles.nth(index)
            info = element.evaluate(
                """(el) => {
                  const tag = el.tagName.toLowerCase();
                  const type = (el.getAttribute('type') || '').toLowerCase();
                  const aria = el.getAttribute('aria-label') || '';
                  const label = el.labels && el.labels.length ? el.labels[0].innerText : '';
                  const text = (el.innerText || el.value || aria || label || '').trim();
                  const role = el.getAttribute('role') ||
                    (tag === 'a' ? 'link' : (tag === 'button' || type === 'submit' ? 'button' : 'input'));
                  return {tag, type, aria, label: label.trim(), text, role,
                    name: el.getAttribute('name') || '', id: el.id || '',
                    href: el.getAttribute('href') || ''};
                }"""
            )
            if not info["text"] and not info["aria"] and not info["label"]:
                continue
            locator = self._stable_locator(info)
            name = info["aria"] or info["label"] or info["text"]
            result.append(Control(f"control-{index}", info["role"], name, locator))
        return result

    def _readable_targets(self) -> list[ReadableTarget]:
        handles = self.page.locator("h1,h2,h3,th,td,p,[id]")
        result: list[ReadableTarget] = []
        seen: set[tuple[str, str]] = set()
        for index in range(handles.count()):
            element = handles.nth(index)
            info = element.evaluate(
                """(el) => ({
                  text: (el.innerText || '').trim(),
                  id: el.id || '',
                  tag: el.tagName.toLowerCase()
                })"""
            )
            if not info["text"]:
                continue
            key = (info["id"], info["text"])
            if key in seen:
                continue
            seen.add(key)
            if info["id"]:
                locator = Locator(
                    "css",
                    f"#{info['id']}",
                    rationale="Stable author-provided id for a readable output target.",
                )
            else:
                locator = Locator(
                    "text",
                    info["text"],
                    rationale="Exact visible text fallback for legacy readable content.",
                )
            result.append(ReadableTarget(f"target-{index}", info["text"], locator))
        return result

    @staticmethod
    def _stable_locator(info: dict[str, str]) -> Locator:
        if info["label"]:
            return Locator(
                "label",
                info["label"],
                rationale="Associated form label is more stable than position or generated markup.",
            )
        if info["role"] in {"button", "link"} and info["text"]:
            return Locator(
                "role",
                f"{info['role']}:{info['text']}",
                rationale="Accessible role and visible name survive table/layout changes.",
            )
        if info["name"]:
            return Locator(
                "css",
                f"{info['tag']}[name={_quote_css(info['name'])}]",
                rationale="Named control fallback for legacy markup without test IDs.",
            )
        if info["id"]:
            return Locator(
                "css",
                f"#{info['id']}",
                rationale="Author-provided element id; used only when semantic metadata is absent.",
            )
        if info["href"]:
            return Locator(
                "css",
                f"a[href={_quote_css(info['href'])}]",
                rationale="Exact route is a stable fallback for a legacy link.",
            )
        raise SurfaceError(f"control has no stable locator: {info}")

    def resolve(self, locator: Locator) -> Any:
        errors: list[str] = []
        for candidate in (locator, *locator.fallback):
            try:
                handle = self._resolve_one(candidate)
                if handle.count() > 0:
                    return handle
                errors.append(f"{candidate.strategy}:{candidate.value} matched 0 elements")
            except Exception as exc:
                errors.append(f"{candidate.strategy}:{candidate.value}: {exc}")
        raise SurfaceError(f"could not resolve locator {locator}: {'; '.join(errors)}")

    def _resolve_one(self, locator: Locator) -> Any:
        try:
            if locator.strategy == "label":
                return self.page.get_by_label(locator.value, exact=True)
            if locator.strategy == "text":
                return self.page.get_by_text(locator.value, exact=True)
            if locator.strategy == "css":
                return self.page.locator(locator.value)
            if locator.strategy == "role":
                role, _, name = locator.value.partition(":")
                return self.page.get_by_role(role, name=name, exact=True)
        except Exception as exc:
            raise SurfaceError(f"could not resolve locator {locator}: {exc}") from exc
        raise SurfaceError(f"unsupported locator strategy: {locator.strategy}")

    def perform(self, action: ActionType, locator: Locator | None = None, value: str | None = None, timeout_ms: int = 5_000) -> None:
        try:
            if action is ActionType.NAVIGATE:
                if value is None:
                    raise SurfaceError("navigate requires a URL")
                self.page.goto(value, wait_until="domcontentloaded", timeout=timeout_ms)
            elif action is ActionType.CLICK:
                if locator is None:
                    raise SurfaceError("click requires a locator")
                self.resolve(locator).click(timeout=timeout_ms)
                self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            elif action is ActionType.FILL:
                if locator is None or value is None:
                    raise SurfaceError("fill requires a locator and value")
                self.resolve(locator).fill(value, timeout=timeout_ms)
            elif action is ActionType.PRESS:
                if locator is None or value is None:
                    raise SurfaceError("press requires a locator and key")
                self.resolve(locator).press(value, timeout=timeout_ms)
            elif action is ActionType.WAIT:
                self.page.wait_for_timeout(int(value or "250"))
            else:
                raise SurfaceError(f"unsupported interactive action: {action.value}")
        except SurfaceError:
            raise
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                raise SurfaceTimeout(f"{action.value} timed out: {exc}") from exc
            raise SurfaceError(f"{action.value} failed: {exc}") from exc

    def extract(self, locator: Locator, timeout_ms: int = 5_000) -> str:
        try:
            return str(self.resolve(locator).inner_text(timeout=timeout_ms)).strip()
        except Exception as exc:
            raise SurfaceError(f"extract failed for {locator}: {exc}") from exc

    def capture(self, directory: Path, stem: str) -> tuple[Path | None, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        screenshot = directory / f"{stem}.png" if os.environ.get("CUA_PERSIST_SCREENSHOTS") == "1" else None
        snapshot = directory / f"{stem}.txt"
        if screenshot is not None:
            # Opt-in only: screenshots can contain regulated data. The default
            # evidence signal is the redacted DOM/accessibility text snapshot.
            self.page.screenshot(path=str(screenshot), full_page=True)
        snapshot.write_text(redact_text(self.observe().text), encoding="utf-8")
        return screenshot, snapshot


def _quote_css(value: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", value) + '"'
