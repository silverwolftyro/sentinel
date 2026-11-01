"""Windows **系统托盘**图标（v3.4.0）。

为什么要有它
============

v3.3.x 的用户撞上的最严重的一件事是：

> 找不到「退出系统」的按钮，关掉黑框后后台变成无法杀死的幽灵，
> 只能去任务管理器杀。

「关掉黑框」在 Windows 上**不会**杀掉控制台程序 ——
进程会继续在后台跑，而用户**找不到任何地方**能把它关掉。

托盘就是那个「随时能按的退出键」：
它在右下角常驻，右键两下就能看到【打开看板】与【彻底退出】。

设计取舍
========

* **图标自己画**（Pillow）—— 不引入 .ico 资源文件，
  打包时少一个二进制依赖，也不用管图标路径在源码/打包两种形态下
  到底指向哪儿；
* **托盘是可选的** —— ``pystray`` / ``Pillow`` 没装就**不出托盘**，
  但哨兵的其它功能**一点不受影响**（与 ``psutil`` 同一个待遇，
  架构红线 R5）；
* **托盘线程是守护线程** —— 主线程一退出，进程就退，
  不会因为托盘还挂着而变成新的「幽灵」。
"""

from __future__ import annotations

import logging
import os
import threading
import webbrowser
from typing import Any, Callable, Optional

__all__ = [
    "TRAY_MENU_OPEN",
    "TRAY_MENU_QUIT",
    "build_tray_icon",
    "stop_tray",
    "tray_available",
]

logger = logging.getLogger(__name__)

#: 托盘菜单第一项：**打开看板**。
TRAY_MENU_OPEN = "打开看板"

#: 托盘菜单最后一项：**彻底退出**。
TRAY_MENU_QUIT = "彻底退出"

#: 图标主色（哨兵的青色，与看板同一套配色）。
BRAND_COLOR = (34, 211, 238)

#: 图标背景色。
BRAND_BG = (9, 14, 26)

#: Windows 自带的中文字体（按优先级找）。
#:
#: ⚠️ 找不到任何一个也不影响出图标 ——
#: 那时画一个纯几何的「眼睛」图案，照样能用。
_CJK_FONT_CANDIDATES = (
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyh.ttc"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyhbd.ttc"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simhei.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simsun.ttc"),
)


def tray_available() -> bool:
    """托盘能不能用。

    :return: ``pystray`` 与 ``Pillow`` 都装上了返回 ``True``。
    """
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def _load_font(size: int) -> Optional[Any]:
    """找一款能写「哨」字的中文字体。

    :param size: 字号。
    :return: Pillow 字体对象；找不到返回 ``None``。
    """
    from PIL import ImageFont

    for path in _CJK_FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def build_tray_image(size: int = 64) -> Optional[Any]:
    """画托盘图标。

    优先画一个青底圆角方块 + 白色「哨」字；
    没有中文字体就画一只**几何的眼睛**（哨兵本来就是「盯梢」的），
    两种都至少是**能认出来**的图标。

    :param size: 边长（像素）。
    :return: Pillow 的 ``RGBA`` 图像；Pillow 缺失时返回 ``None``。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = max(1, size // 16)
    radius = max(2, size // 5)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius,
        fill=BRAND_BG + (255,),
        outline=BRAND_COLOR + (255,),
        width=max(1, size // 32),
    )

    font = _load_font(int(size * 0.62))
    if font is not None:
        text = "哨"
        try:
            box = draw.textbbox((0, 0), text, font=font)
            width = box[2] - box[0]
            height = box[3] - box[1]
            draw.text(
                ((size - width) / 2 - box[0], (size - height) / 2 - box[1]),
                text,
                font=font,
                fill=(240, 253, 255, 255),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "画托盘图标文字失败：%s: %s", type(exc).__name__, exc
            )
    else:
        # 没有中文字体：画一只眼睛。
        eye = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        eye_draw = ImageDraw.Draw(eye)
        eye_draw.ellipse(
            [size * 0.18, size * 0.32, size * 0.82, size * 0.68],
            fill=(240, 253, 255, 255),
        )
        eye_draw.ellipse(
            [size * 0.40, size * 0.40, size * 0.60, size * 0.60],
            fill=BRAND_BG + (255,),
        )
        image = Image.alpha_composite(image, eye)
    return image


def build_tray_icon(
    url: str,
    on_quit: Callable[[], None],
    *,
    title: str = "哨兵 Sentinel",
) -> Optional[Any]:
    """建一个托盘图标。

    菜单只有两项，**故意不放别的**：

    * 【打开看板】—— 打开浏览器；
    * 【彻底退出】—— 走 :mod:`src.gui.shutdown` 那条统一出口。

    :param url: 看板地址（用于【打开看板】）。
    :param on_quit: 点【彻底退出】时调用的回调。
    :param title: 悬浮提示文字。
    :return: pystray 的 ``Icon``；不可用时返回 ``None``。
    """
    try:
        import pystray
    except ImportError:
        logger.info("没有装 pystray —— 不出托盘图标。")
        return None

    image = build_tray_image()
    if image is None:
        logger.info("没有装 Pillow —— 不出托盘图标。")
        return None

    def _open_dashboard(
        icon: Any = None, item: Any = None
    ) -> None:
        """打开看板。"""
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 —— 开浏览器失败不该崩托盘
            logger.warning(
                "打开看板失败：%s: %s", type(exc).__name__, exc
            )

    def _quit(icon: Any = None, item: Any = None) -> None:
        """彻底退出。"""
        try:
            on_quit()
        except Exception as exc:  # noqa: BLE001 —— 退出失败不该崩托盘
            logger.warning(
                "托盘退出回调失败：%s: %s", type(exc).__name__, exc
            )

    menu = pystray.Menu(
        pystray.MenuItem(
            TRAY_MENU_OPEN, _open_dashboard, default=True
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(TRAY_MENU_QUIT, _quit),
    )
    icon = pystray.Icon("sentinel", image, title, menu)
    return icon


def start_tray(icon: Any) -> Optional[threading.Thread]:
    """把托盘图标跑起来（**守护线程**）。

    ⚠️ 为什么不用 ``icon.run_detached()`` ——
    它起的是**非守护**线程，进程会因此**赖着不走**。
    那就等于用一个新的「幽灵」替换掉旧的「幽灵」。

    :param icon: pystray 的 ``Icon``。
    :return: 跑图标的那个线程；出错返回 ``None``。
    """
    if icon is None:
        return None
    try:
        thread = threading.Thread(
            target=icon.run, name="sentinel-tray", daemon=True
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001 —— 托盘起不来不该影响主功能
        logger.warning(
            "启动托盘图标失败：%s: %s", type(exc).__name__, exc
        )
        return None
    return thread


def stop_tray(icon: Optional[Any]) -> None:
    """把托盘图标收掉。

    :param icon: pystray 的 ``Icon``；``None`` 直接返回。
    """
    if icon is None:
        return
    try:
        icon.stop()
    except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
        logger.warning(
            "停止托盘图标失败：%s: %s", type(exc).__name__, exc
        )
