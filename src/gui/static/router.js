/**
 * 前端路由（v2.3.0）—— 原生 History API。
 *
 * 为什么要它
 * ==========
 *
 * v2.2.x 之前哨兵只有一个页面，功能全堆在上面：
 * 顶栏一堆按钮、弹窗、模态框……用户抱怨「界面元素堆砌过多」。
 *
 * v2.3.0 把它拆成**两个真正的页面**：
 *
 * * `/` —— 主看板（实时监控 / 告警流 / 时间线）；
 * * `/settings` —— 独立全屏的设置页。
 *
 * 设置页**不是**抽屉、**不是**弹窗、**不是**模态框 ——
 * 它有自己的 URL，可以直接收藏、直接分享、刷新也不丢。
 *
 * 怎么做到「不整页刷新」
 * ====================
 *
 * 1. 拦截 ``a[data-nav]`` 的点击；
 * 2. ``fetch`` 目标页面 → 用 ``DOMParser`` 解析；
 * 3. 换掉 ``document.body`` 的内容；
 * 4. 重新执行新页面自己的 ``<script src>``；
 * 5. 调用新页面的 ``init()``。
 *
 * 页面契约
 * ========
 *
 * 每个页面脚本要暴露 ``window.SentinelPage = { init, destroy }``：
 *
 * * ``init()`` —— 绑事件、拉首屏数据；
 * * ``destroy()`` —— 收摊：关掉 EventSource、清掉定时器。
 *
 * ⚠️ 不收摊的话，离开看板以后后台还在轮询、还在收 SSE ——
 * 那既费电，又会在设置页上冒出看板的日志。
 *
 * 兜底
 * ====
 *
 * 万一 ``fetch`` 失败（比如后端重启了），直接
 * ``window.location.href = url`` 整页跳 —— 宁可慢一点，也不能卡死。
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* 路由表                                                              */
  /* ------------------------------------------------------------------ */

  var ROUTES = {
    "/": { title: "哨兵 Sentinel · 实时监控" },
    "/settings": { title: "哨兵 Sentinel · 设置" },
    // v3.0.2：设置页改成真正的**多页面应用** ——
    // 每个设置功能一个独立 URL，点菜单**地址真的会变**。
    "/settings/llm": { title: "哨兵 Sentinel · 设置 · 模型路由" },
    "/settings/webhook": { title: "哨兵 Sentinel · 设置 · 通知通道" },
    // v3.7.0：流水线控制（独立成页）——
    // L1 / L2 / L3 独立开关、L2→L3 桥接、L1 命中后的拦截策略，
    // 以及「呈现与摘要」的两个极限压缩档位。
    "/settings/pipeline": { title: "哨兵 Sentinel · 设置 · 流水线控制" },
    "/settings/rules": { title: "哨兵 Sentinel · 设置 · L1 规则" },
    "/settings/sessions": { title: "哨兵 Sentinel · 设置 · 挂载管理" }
  };

  /**
   * 当前路由（归一化后的路径）。
   *
   * @return {string} 形如 "/" 或 "/settings"。
   */
  function routeOf() {
    var path = window.location.pathname || "/";
    if (path.length > 1) {
      path = path.replace(/\/+$/, "");
    }
    return path || "/";
  }

  /**
   * 取当前页面的生命周期钩子。
   *
   * @return {?Object} 页面脚本暴露的 ``window.SentinelPage``。
   */
  function pageHook() {
    return window.SentinelPage || null;
  }

  /**
   * 拆掉当前页面（收摊）。
   */
  function teardown() {
    var hook = pageHook();
    if (hook && typeof hook.destroy === "function") {
      try {
        hook.destroy();
      } catch (err) {
        console.error("页面收摊失败：", err);
      }
    }
  }

  /**
   * 加载一个脚本（等它执行完）。
   *
   * ⚠️ 用 ``appendChild`` 插入的脚本默认是异步执行的 ——
   * 必须等 ``onload``，否则可能在页面脚本还没跑完时就调 ``init()``。
   *
   * @param {string} src 脚本地址。
   * @return {Promise} 加载完成（成功或失败都 resolve）。
   */
  function loadScript(src) {
    return new Promise(function (resolve) {
      var fresh = document.createElement("script");
      fresh.src = src;
      fresh.async = false;
      fresh.onload = function () { resolve(); };
      fresh.onerror = function () { resolve(); };
      document.body.appendChild(fresh);
    });
  }

  /**
   * 换掉页面级样式（``<style>`` 与 ``<link rel=stylesheet>``）。
   *
   * ⚠️ 为什么必须连 head 一起换：
   * 设置页在 ``<head>`` 里带了自己的一小段 ``<style>``
   * （它自己排版，不跟看板共用）。
   * 只换 body 的话，从看板跳过去时那段样式会**留在 head 里**——
   * 页面看着像设置页，排版却是看板的，宽度全乱。
   *
   * ``<script>`` 不在这里换 —— 路由器自己住在 head 里，换掉它就死了。
   *
   * @param {Document} doc 解析好的新页面。
   */
  function swapHead(doc) {
    var head = document.head;
    if (!head) {
      return;
    }
    var stale = head.querySelectorAll("style, link[rel='stylesheet']");
    Array.prototype.forEach.call(stale, function (node) {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    });
    var fresh = doc.head.querySelectorAll("style, link[rel='stylesheet']");
    Array.prototype.forEach.call(fresh, function (node) {
      head.appendChild(node.cloneNode(true));
    });
  }

  /**
   * 同步 body 的**属性**（class / data-*）。
   *
   * ⚠️ 为什么必须单独做（v3.9.0 修的坑）：
   *    ``innerHTML`` **只换子节点，不换属性**。
   *    于是从看板点进设置页之后，body 上还挂着看板的
   *    ``class="is-dashboard"`` 和 ``data-mobile-tab="alerts"``，
   *    而设置页自己的 ``class="settings-body"`` **从来没生效过** ——
   *    「从看板点进来」和「直接打开设置页」长得不一样。
   *    移动端 Tab 的显隐规则挂在 body 的 data-* 上，
   *    不修这里，设置页在窄屏下会被看板的规则误伤。
   *
   * @param {Document} doc 解析好的新页面。
   * @return {void}
   */
  function swapBody(doc) {
    var next = doc.body;
    var current = document.body;
    if (!next || !current) {
      return;
    }
    var i;
    var name;
    var attr;
    // 先收集「当前有、新页面没有」的属性名 —— 收集完再动 DOM，
    // 免得边遍历边改。
    var stale = [];
    for (i = 0; i < current.attributes.length; i += 1) {
      name = current.attributes[i].name;
      if (name !== "class" && !next.hasAttribute(name)) {
        stale.push(name);
      }
    }
    for (i = 0; i < stale.length; i += 1) {
      current.removeAttribute(stale[i]);
    }
    for (i = 0; i < next.attributes.length; i += 1) {
      attr = next.attributes[i];
      current.setAttribute(attr.name, attr.value);
    }
  }

  /**
   * 用新页面的内容替换当前页面。
   *
   * @param {Document} doc 解析好的新页面。
   * @return {Promise} 换页完成。
   */
  function swap(doc) {
    document.title = doc.title;
    swapHead(doc);
    swapBody(doc);
    document.body.innerHTML = doc.body.innerHTML;

    var scripts = doc.querySelectorAll("body script[src]");
    var chain = Promise.resolve();
    Array.prototype.forEach.call(scripts, function (old) {
      chain = chain.then(function () {
        return loadScript(old.getAttribute("src"));
      });
    });

    return chain.then(function () {
      var hook = pageHook();
      if (hook && typeof hook.init === "function") {
        try {
          hook.init();
        } catch (err) {
          console.error("页面初始化失败：", err);
        }
      }
      window.scrollTo(0, 0);
    });
  }

  /**
   * 导航到某个地址。
   *
   * @param {string} url 目标地址（站内绝对路径）。
   * @param {boolean} push 是否往历史里压一条（浏览器后退要能用）。
   */
  function navigate(url, push) {
    // ⚠️ v3.0.0：**先收摊，再去 fetch**。
    //
    // 原来的顺序是「fetch → 收摊 → 换页」，看着没毛病，
    // 实际上有个真实的坑：Agent 跑得飞起时看板每秒能收几百行，
    // 那些 SSE 回调全是**主线程**上的 DOM 操作 ——
    // fetch 的 ``.then()`` 排在事件循环里，只能等它们跑完。
    // 于是「点【⚙ 设置】→ 页面卡半秒才动」。
    //
    // 先关掉 EventSource，主线程立刻空出来，
    // 切页就是瞬间的事。
    teardown();
    fetch(url, { headers: { "X-Requested-With": "sentinel-router" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, "text/html");
        if (push) {
          window.history.pushState({ url: url }, "", url);
        }
        // 告诉新页面脚本「别自己初始化，路由器会统一调」——
        // 否则会初始化两遍（事件绑两遍、定时器跑两轮）。
        window.__SENTINEL_ROUTING__ = true;
        return swap(doc).then(function () {
          window.__SENTINEL_ROUTING__ = false;
        });
      })
      .catch(function (err) {
        console.error("页面导航失败，回退到整页跳转：", err);
        window.location.href = url;
      });
  }

  /**
   * 拦截站内导航链接的点击。
   *
   * @param {MouseEvent} event 事件。
   */
  function onClick(event) {
    if (event.defaultPrevented || event.button !== 0) {
      return;
    }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    var node = event.target;
    var anchor = null;
    while (node && node !== document) {
      if (node.tagName === "A" && node.hasAttribute("data-nav")) {
        anchor = node;
        break;
      }
      node = node.parentNode;
    }
    if (!anchor) {
      return;
    }
    var href = anchor.getAttribute("href");
    if (!href || href.charAt(0) !== "/") {
      return;
    }
    event.preventDefault();
    if (href === routeOf()) {
      return;
    }
    navigate(href, true);
  }

  /**
   * 路由器的启动。
   */
  function init() {
    document.addEventListener("click", onClick);
    window.addEventListener("popstate", function () {
      navigate(routeOf(), false);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.SentinelRouter = {
    routes: ROUTES,
    navigate: navigate,
    routeOf: routeOf,
    teardown: teardown
  };
})();
