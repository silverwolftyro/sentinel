/* 哨兵 Sentinel —— 设置子页面：关于系统（v3.2.0）
 *
 * 这一页只做一件事：把哨兵**自己**的运行状况摊开给用户看。
 *
 * ⚠️ 为什么要单独占一个页面
 * ----------------------
 * 「监控工具本身不能变成资源消耗大户」——
 * 用户得能**看见**它吃了多少 CPU / 内存，
 * 否则「哨兵半夜把机器拖垮了」这种事永远说不清。
 *
 * 页面契约：暴露 ``window.SentinelPage = { init, destroy }``。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;

  /* 每 5 秒刷新一次。
   *
   * ⚠️ 不是「越高频越好」——
   * 采样要调 Win32 API，太频繁反而是负载。
   * 5 秒足够看出「内存是不是在涨」。 */
  var REFRESH_MS = 5000;

  var timer = null;

  /* ---------------------------------------------------------- 渲染 */

  function memoryText(value) {
    if (value === null || value === undefined) {
      return "未知";
    }
    return Number(value).toFixed(1) + " MB";
  }

  function render(data) {
    var process = data.process || {};
    C.setText("system-version", data.version || "–");
    C.setText("system-uptime", process.uptime_text || "未知");
    C.setText("system-server-uptime", data.server_uptime_text || "未知");
    C.setText("system-memory", memoryText(process.memory_mb));
    C.setText("system-memory-peak", memoryText(process.memory_peak_mb));
    /* CPU 百分比要两次采样才有意义 ——
     * 第一次是「还没采到」，别显示 0.0% 骗人。 */
    C.setText(
      "system-cpu",
      process.cpu_ready
        ? Number(process.cpu_percent).toFixed(1) + " %"
        : "采样中…"
    );
    C.setText(
      "system-pid",
      process.pid === undefined ? "–" : String(process.pid)
    );
    C.setText(
      "system-threads",
      process.threads === undefined ? "–" : String(process.threads)
    );
    C.setText(
      "system-sessions",
      data.sessions === undefined ? "–" : String(data.sessions) + " 路"
    );
    C.setText("system-bind", data.bind || "–");
  }

  /* ---------------------------------------------------------- 拉数据 */

  function refresh() {
    return fetch("/api/system")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          C.setText(
            "system-status",
            "读取失败：" + ((data && data.error) || "未知错误")
          );
          return;
        }
        render(data);
        C.setText("system-status", "已更新");
      })
      .catch(function (err) {
        C.setText("system-status", "读取失败：" + err.message);
      });
  }

  /* 这一页是**只读**的 —— 【保存并应用】与【重新载入】
   * 在这里没有任何意义，直接藏掉，免得用户点了没反应。 */
  function hideWriteButtons() {
    ["save-button", "reload-button"].forEach(function (id) {
      var node = C.byId(id);
      if (node && node.parentNode) {
        node.parentNode.style.display = "none";
      }
    });
  }

  /* ---------------------------------------------------------- 生命周期 */

  function init() {
    hideWriteButtons();
    refresh();
    timer = window.setInterval(refresh, REFRESH_MS);
  }

  function destroy() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  window.SentinelPage = { init: init, destroy: destroy };
  C.boot(init);
})();
