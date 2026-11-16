from __future__ import annotations

import asyncio
import os
from io import BytesIO
from typing import Any


class BrowserTools:
    """Small, async browser and image helpers for trusted automation nodes."""

    def __init__(self, page: Any, lock: asyncio.Lock):
        self._page = page
        self._lock = lock

    def _ensure_page(self) -> None:
        if self._page is None or self._page.is_closed():
            raise RuntimeError("当前任务浏览器页面不可用")

    async def screenshot(self, *, full_page: bool = False, selector: str | None = None) -> bytes:
        """Capture the viewport, the full page, or one CSS-selected element as PNG bytes."""
        if selector and full_page:
            raise ValueError("指定 selector 时不能同时设置 full_page")
        async with self._lock:
            self._ensure_page()
            if selector:
                return await self._page.locator(selector).screenshot(type="png", animations="disabled")
            return await self._page.screenshot(type="png", full_page=full_page, animations="disabled")

    async def screenshot_region(self, *, x_ratio: float = 0, y_ratio: float = 0,
                                width_ratio: float = 0.55, height_ratio: float = 0.35) -> bytes:
        """Capture only a viewport region expressed as ratios of viewport dimensions."""
        ratios = (x_ratio, y_ratio, width_ratio, height_ratio)
        if any(not 0 <= ratio <= 1 for ratio in ratios):
            raise ValueError("截图区域比例必须在 0 到 1 之间")
        if width_ratio == 0 or height_ratio == 0 or x_ratio + width_ratio > 1 or y_ratio + height_ratio > 1:
            raise ValueError("截图区域必须位于浏览器视口范围内且宽高大于 0")
        async with self._lock:
            self._ensure_page()
            viewport = self._page.viewport_size
            if not viewport:
                raise RuntimeError("无法读取当前浏览器视口尺寸")
            return await self._page.screenshot(
                type="png",
                clip={
                    "x": viewport["width"] * x_ratio,
                    "y": viewport["height"] * y_ratio,
                    "width": viewport["width"] * width_ratio,
                    "height": viewport["height"] * height_ratio,
                },
                animations="disabled",
            )

    async def click(self, x: float, y: float, *, button: str = "left", click_count: int = 1) -> None:
        if button not in {"left", "middle", "right"}:
            raise ValueError("button 必须是 left、middle 或 right")
        if click_count < 1:
            raise ValueError("click_count 必须大于 0")
        async with self._lock:
            self._ensure_page()
            await self._page.mouse.click(float(x), float(y), button=button, click_count=int(click_count))

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        if steps < 1:
            raise ValueError("steps 必须大于 0")
        async with self._lock:
            self._ensure_page()
            await self._page.mouse.move(float(x), float(y), steps=int(steps))

    async def drag(self, start_x: float, start_y: float, end_x: float, end_y: float, *, steps: int = 10,
                   button: str = "left") -> None:
        if button not in {"left", "middle", "right"}:
            raise ValueError("button 必须是 left、middle 或 right")
        if steps < 1:
            raise ValueError("steps 必须大于 0")
        async with self._lock:
            self._ensure_page()
            await self._page.mouse.move(float(start_x), float(start_y))
            await self._page.mouse.down(button=button)
            try:
                await self._page.mouse.move(float(end_x), float(end_y), steps=int(steps))
            finally:
                await self._page.mouse.up(button=button)

    async def scroll(self, delta_y: float, *, delta_x: float = 0, x: float | None = None,
                     y: float | None = None) -> None:
        if (x is None) != (y is None):
            raise ValueError("定位滚动位置时必须同时提供 x 和 y")
        async with self._lock:
            self._ensure_page()
            if x is not None and y is not None:
                await self._page.mouse.move(float(x), float(y))
            await self._page.mouse.wheel(float(delta_x), float(delta_y))

    async def press(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("key 不能为空")
        async with self._lock:
            self._ensure_page()
            await self._page.keyboard.press(key)

    async def hotkey(self, *keys: str) -> None:
        if not keys or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("至少需要一个有效按键")
        async with self._lock:
            self._ensure_page()
            await self._page.keyboard.press("+".join(keys))

    async def type_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        async with self._lock:
            self._ensure_page()
            await self._page.keyboard.insert_text(text)

    async def ocr(self, image: bytes | None = None, *, lang: str = "eng", config: str = "") -> dict[str, Any]:
        """Recognize text and viewport-relative boxes; capture the current viewport when image is omitted."""
        if image is None:
            image = await self.screenshot()
        if not isinstance(image, (bytes, bytearray, memoryview)):
            raise TypeError("image 必须是截图返回的 bytes")
        return await asyncio.to_thread(_ocr_image, bytes(image), lang, config)

    async def find_image(self, template: bytes, *, image: bytes | None = None,
                         threshold: float = 0.8) -> dict[str, Any]:
        """Find a fixed-scale template in a viewport screenshot using OpenCV."""
        if not isinstance(template, (bytes, bytearray, memoryview)):
            raise TypeError("template 必须是图片 bytes")
        if image is None:
            image = await self.screenshot()
        if not isinstance(image, (bytes, bytearray, memoryview)):
            raise TypeError("image 必须是截图返回的 bytes")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold 必须在 0 到 1 之间")
        return await asyncio.to_thread(_match_template, bytes(image), bytes(template), float(threshold))


def _ocr_image(image: bytes, lang: str, config: str) -> dict[str, Any]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("OCR 依赖未安装；请安装 requirements.txt 中的 pytesseract 和 Pillow") from error

    # Tesseract itself is an OS package, not a Python dependency. Allow an
    # explicit executable path for Windows installs that are not on PATH.
    tesseract_cmd = os.environ.get("TESSERACT_CMD")
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    try:
        data = pytesseract.image_to_data(Image.open(BytesIO(image)), lang=lang, config=config,
                                         output_type=pytesseract.Output.DICT)
    except pytesseract.pytesseract.TesseractNotFoundError as error:
        raise RuntimeError("未找到 Tesseract OCR 引擎；请安装引擎并配置 PATH，或设置 TESSERACT_CMD 指向可执行文件") from error
    except pytesseract.TesseractError as error:
        raise RuntimeError(f"OCR 执行失败：{error}") from error

    boxes = []
    for index, raw_text in enumerate(data["text"]):
        text = raw_text.strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1.0
        boxes.append({
            "text": text,
            "confidence": confidence,
            "x": int(data["left"][index]),
            "y": int(data["top"][index]),
            "width": int(data["width"][index]),
            "height": int(data["height"][index]),
        })
    return {"text": " ".join(box["text"] for box in boxes), "boxes": boxes}


def _match_template(image: bytes, template: bytes, threshold: float) -> dict[str, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise RuntimeError("图像识别依赖未安装；请安装 requirements.txt 中的 opencv-python-headless") from error

    screenshot = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    target = cv2.imdecode(np.frombuffer(template, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if screenshot is None or target is None:
        raise ValueError("截图或模板不是有效的图片")
    height, width = target.shape[:2]
    if height > screenshot.shape[0] or width > screenshot.shape[1]:
        return {"found": False, "confidence": 0.0, "x": None, "y": None, "width": width, "height": height}
    scores = cv2.matchTemplate(screenshot, target, cv2.TM_SQDIFF_NORMED)
    score, _, location, _ = cv2.minMaxLoc(scores)
    confidence = max(0.0, min(1.0, 1.0 - float(score)))
    found = confidence >= threshold
    return {
        "found": found,
        "confidence": confidence,
        "x": int(location[0]) if found else None,
        "y": int(location[1]) if found else None,
        "width": width,
        "height": height,
    }
