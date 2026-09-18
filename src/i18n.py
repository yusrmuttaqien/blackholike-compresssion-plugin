# ── i18n.py · Language resolution + translation lookup ────────────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


TRANSLATIONS = {
    "en-US": {
        "status_context_usage": "Context Usage (Estimated): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_history_usage": "History Usage (Estimated): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_compaction_drives": " | (drives compaction)",
        "status_high_usage": " | ⚠️ High Usage",
        "status_loaded_summary": "Loaded historical summary (Hidden {count} historical messages)",
        "status_context_summary_updated": "Context Summary Updated: {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_generating_summary": "Generating context summary in background...",
        "status_summary_error": "Summary Error: {error} | Check browser console (F12) for details",
        "status_external_refs_injected": "Bypassed chat RAG and injected {count} referenced chat context(s)",
        "summary_prompt_prefix": "【Previous Summary: The following is a summary of the historical conversation, provided for context only. Do not reply to the summary content itself; answer the subsequent latest questions directly.】\n\n",
        "summary_prompt_suffix": "\n\n---\nBelow is the recent conversation:",
        "tool_trimmed": "... [Tool outputs trimmed]\n{content}",
        "content_collapsed": "\n... [Content collapsed] ...\n",
    },
    "zh-CN": {
        "status_context_usage": "上下文用量 (预估): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_history_usage": "对话历史用量 (预估): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_compaction_drives": " | (触发压缩)",
        "status_high_usage": " | ⚠️ 用量较高",
        "status_loaded_summary": "已加载历史总结 (隐藏了 {count} 条历史消息)",
        "status_context_summary_updated": "上下文总结已更新: {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_generating_summary": "正在后台生成上下文总结...",
        "status_summary_error": "总结生成错误: {error} | 请查看浏览器控制台(F12)获取详情",
        "status_external_refs_injected": "已绕过 chat RAG，并注入 {count} 个引用聊天上下文",
        "summary_prompt_prefix": "【前情提要：以下是历史对话的总结，仅供上下文参考。请不要回复总结内容本身，直接回答之后最新的问题。】\n\n",
        "summary_prompt_suffix": "\n\n---\n以下是最近的对话：",
        "tool_trimmed": "... [工具输出已裁剪]\n{content}",
        "content_collapsed": "\n... [内容已折叠] ...\n",
    },
}

class I18nMixin:

    """Language resolution + translation lookup."""

    def _resolve_language(self, lang: str) -> str:
        """Resolve the best matching language code from the TRANSLATIONS dict."""
        target_lang = lang

        # 1. Direct match
        if target_lang in TRANSLATIONS:
            return target_lang

        # 2. Variant fallback (explicit mapping)
        if target_lang in self.fallback_map:
            target_lang = self.fallback_map[target_lang]
            if target_lang in TRANSLATIONS:
                return target_lang

        # 3. Base language fallback (e.g. fr-BE -> fr-FR)
        if "-" in lang:
            base_lang = lang.split("-")[0]
            for supported_lang in TRANSLATIONS:
                if supported_lang.startswith(base_lang + "-"):
                    return supported_lang

        # 4. Final Fallback to en-US
        return "en-US"

    def _get_translation(self, lang: str, key: str, **kwargs) -> str:
        """Get translated string for the given language and key."""
        target_lang = self._resolve_language(lang)
        lang_dict = TRANSLATIONS.get(target_lang, TRANSLATIONS["en-US"])
        text = lang_dict.get(key, TRANSLATIONS["en-US"].get(key, key))
        if kwargs:
            try:
                text = text.format(**kwargs)
            except Exception as e:
                logger.warning(f"Translation formatting failed for {key}: {e}")
        return text
