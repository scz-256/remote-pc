"""被操作那台电脑上跑的 agent。

这里之前没有 `__init__.py`，所以只能 `python agent/agent.py` 直接跑，没法
`import agent.agent`。图形界面要用 `Agent` 类，所以补上这一个文件。

`agent/agent.py` 里没有任何相对导入（本来就要能单独跑），所以补了它
**不影响**原来的用法：

    python agent/agent.py --server ws://10.22.114.127:8765 --token <密钥>

补它也是 PyInstaller 能静态分析到这个模块的前提：靠 `spec_from_file_location`
之类的动态加载，打包时会跟丢，最后表现为"exe 里少了个模块"。
"""
