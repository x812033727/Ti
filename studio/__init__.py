"""Ti Studio — AI 專家討論工作室。

由多位 AI 專家（專案經理、工程師、高級工程師、驗證工程師）組成的自主軟體開發工作室，
能自己寫程式、互相討論、自我改進、自我檢討，並透過網頁即時呈現協作過程。
"""

# 版本字串單一事實來源為 pyproject.toml（見 DECISIONS）。
# 此處不再硬寫 __version__，下游需要時以 tomllib 讀取 pyproject.toml，避免兩處不一致。

from . import secure_write as secure_write  # re-export; 'as' 是 ruff F401 慣用消法，請勿移除
