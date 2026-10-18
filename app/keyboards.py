from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def submission_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="確定發布", callback_data=f"submit:yes:{nonce}"),
                InlineKeyboardButton(text="取消發布", callback_data=f"submit:no:{nonce}"),
            ]
        ]
    )


def delete_keyboard(nonce: str, article_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="確定刪除",
                    callback_data=f"delete:yes:{article_id}:{nonce}",
                ),
                InlineKeyboardButton(
                    text="取消刪除",
                    callback_data=f"delete:no:{article_id}:{nonce}",
                ),
            ]
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="今日／總投稿數", callback_data="admin:stats"),
                InlineKeyboardButton(text="查詢文章", callback_data="admin:search"),
            ],
            [
                InlineKeyboardButton(text="封鎖作者", callback_data="admin:block"),
                InlineKeyboardButton(text="解除封鎖", callback_data="admin:unblock"),
            ],
            [InlineKeyboardButton(text="封鎖名單", callback_data="admin:list")],
            [
                InlineKeyboardButton(text="待處理事項", callback_data="admin:pending"),
                InlineKeyboardButton(text="暫停／恢復投稿", callback_data="admin:pause"),
            ],
            [InlineKeyboardButton(text="立即備份資料庫", callback_data="admin:backup")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="返回管理面板", callback_data="admin:back")]
        ]
    )


def admin_article_keyboard(article_id: str, needs_review: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="強制刪除文章", callback_data=f"admin:force:{article_id}")],
        [InlineKeyboardButton(text="封鎖這位作者", callback_data=f"admin:block:{article_id}")],
        [InlineKeyboardButton(text="結案此文的檢舉", callback_data=f"admin:close_reports:{article_id}")],
    ]
    if needs_review:
        rows.append([InlineKeyboardButton(text="人工核對結案", callback_data=f"admin:resolve:{article_id}")])
    rows.append([InlineKeyboardButton(text="返回管理面板", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_force_keyboard(article_id: str, nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="確定強制刪除",
                    callback_data=f"admin:force_yes:{article_id}:{nonce}",
                ),
                InlineKeyboardButton(text="取消", callback_data="admin:back"),
            ]
        ]
    )


def page_buttons(prefix: str, page: int, has_next: bool) -> list[InlineKeyboardButton]:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="上一頁", callback_data=f"{prefix}:{page - 1}"))
    if has_next:
        buttons.append(InlineKeyboardButton(text="下一頁", callback_data=f"{prefix}:{page + 1}"))
    return buttons


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="我的投稿", callback_data="mine:page:0")],
        [InlineKeyboardButton(text="檢舉方式", callback_data="help:report"),
         InlineKeyboardButton(text="匿名說明", callback_data="help:privacy")],
    ])


def admin_resolution_keyboard(article_id: str, nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="已確認群組／頻道內無此文，結案", callback_data=f"admin:resolve_yes:{article_id}:{nonce}")],
        [InlineKeyboardButton(text="取消", callback_data="admin:back")],
    ])
