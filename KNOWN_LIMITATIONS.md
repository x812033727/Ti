# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 在 OpenAIExpert.speak() 將 _chat 工具迴圈包進 attempt_fn 並接 run_with_retries(**make_retry_config().as_kwargs())，掛 on_api_error/on_rate_limit_exhausted 回退為空字串
