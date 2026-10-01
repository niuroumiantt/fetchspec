# 厂商官网可达性（2026-10-01）

按 `plan` 里"缺适配器"被点名最多的顺序逐家探查。原则：遵守 robots，不绕登录、不绕反爬；拿不到的写清原因，转浏览器或人工通道，不伪装客户端。

| 厂商 | 点名行数 | 结果 | 去向 |
|---|---:|---|---|
| Siemens Energy | 10（与 Siemens 合计） | 可达；产品页有原生表 | [适配器](siemens-energy.md)，3 行绑定 |
| Siemens（siemens.com） | 同上 | 可达；规格在样本 PDF | [适配器](siemens.md)，3 行绑定 |
| Micron | 6 | 可达；零件规格在官方 JSON 组件 | [适配器](micron.md)，3 行绑定 |
| Samsung 半导体 | 6 | `semiconductor.samsung.com/robots.txt` 返回 403 | 浏览器通道 |
| ABB | 5 | `new.abb.com/robots.txt` 返回 403 | 浏览器通道 |
| Delta | 11 | 可达；规格在页面负载与 Delta 发布的规格文档 | [适配器](delta.md)，6 行绑定 |
| Schneider Electric | 17 | `www.se.com/robots.txt` 可读，但产品页（如 Galaxy VX、NetShelter SX）与 `sitemap.xml` 一律 403 "Access Denied"（反爬拦截） | 浏览器通道 |
| Eaton | 10 | 从云端容器连 `www.eaton.com`：代理隧道建立后 TLS 握手无响应（curl HTTP/2 被重置，HTTP/1.1 与 Python 均超时），连 robots.txt 都取不到 | 在 macmini 上重测；仍不通则转浏览器通道 |

浏览器通道（目标表机制 `js_page`）尚未实现：需要人工辅助、单独授权，执行机是 macmini。上表 403 的厂商在那之前不进入自动采集。
