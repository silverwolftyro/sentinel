/* 哨兵 Sentinel —— 设置子页面：模型路由（L2）（v3.0.2）
 *
 * 只管这一页的字段：**llm** 与 **routing**。
 * 别的页面的字段一概不碰 —— 各页各存，互不覆盖。
 *
 * v3.5.0：多管一块 **cascade**（级联审核）。
 * L2 本地与 L3 云端**分开配置** —— 两者的地址、模型、密钥、
 * 超时完全不是一回事，混在一起必然配错。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;
  var secrets = { llmApiKey: "", fallbackApiKey: "", cascadeL3ApiKey: "" };

  /* -------------------------------------------------------- 小工具 */

  /**
   * 数字输入框留空 = **不写进配置**。
   *
   * ⚠️ 不能用 ``C.num`` —— 它把空值变成 0，
   *    而 0 会把配置里的默认值**顶掉**
   *    （比如 L2 超时写成 0，请求会立刻超时）。
   *
   * @param {string} id 输入框 ID。
   * @return {number|undefined} 数值；留空时返回 ``undefined``
   *         （JSON 序列化时会**整个键消失**，服务端就跳过它）。
   */
  function numOrSkip(id) {
    var raw = C.value(id);
    if (raw === "") {
      return undefined;
    }
    var parsed = Number(raw);
    return isFinite(parsed) ? parsed : undefined;
  }

  /**
   * 文本输入框留空 = **不写进配置**（同上，不能把已配的值抹掉）。
   *
   * @param {string} id 输入框 ID。
   * @return {string|undefined} 文本；留空时返回 ``undefined``。
   */
  function textOrSkip(id) {
    var raw = C.value(id);
    return raw === "" ? undefined : raw;
  }

  /* -------------------------------------------------------- 表单读取 */

  function collectPayload() {
    return {
      llm: {
        provider: C.value("llm-provider"),
        base_url: C.value("llm-base-url"),
        model: C.value("llm-model"),
        timeout: C.num("llm-timeout"),
        max_context_chars: C.num("llm-max-context-chars"),
        risk_threshold: C.num("llm-risk-threshold"),
        redact_before_llm: C.checked("llm-redact-before-llm"),
        keep_alive: C.num("llm-keep-alive"),
        failure_threshold: C.num("llm-failure-threshold"),
        breaker_open_seconds: C.num("llm-breaker-open-seconds"),
        temperature: C.num("llm-temperature"),
        max_tokens: C.num("llm-max-tokens"),
        json_mode: C.checked("llm-json-mode"),
        api_key: C.readSecret("llm-api-key", secrets.llmApiKey)
      },
      routing: {
        enabled: C.checked("routing-enabled"),
        escalate_on_red_line: C.checked("routing-escalate"),
        drift_escalate_threshold: C.num("routing-drift-threshold"),
        fallback: {
          provider: C.value("fallback-provider"),
          base_url: C.value("fallback-base-url"),
          model: C.value("fallback-model"),
          api_key: C.readSecret(
            "fallback-api-key",
            secrets.fallbackApiKey
          )
        }
      },
      // v3.5.0：级联审核 —— L2 本地过滤 + L3 云端终审。
      //
      // ⚠️ v3.7.0：**总开关**（cascade.enabled）与 L2 / L3 的独立开关
      //    已搬到「流水线控制」页 —— 本页不再碰它们，
      //    否则两页各写一半、用户永远不知道开关到底在哪儿。
      cascade: {
        require_agreement: C.checked("cascade-require-agreement"),
        breaker_threshold: numOrSkip("cascade-breaker-threshold"),
        breaker_open_seconds: numOrSkip("cascade-breaker-open-seconds"),
        l2: {
          provider: C.value("cascade-l2-provider"),
          base_url: textOrSkip("cascade-l2-base-url"),
          model: textOrSkip("cascade-l2-model"),
          timeout: numOrSkip("cascade-l2-timeout"),
          timeout_floor: numOrSkip("cascade-l2-timeout-floor"),
          max_tokens: numOrSkip("cascade-l2-max-tokens"),
          reasoning_effort: C.value("cascade-l2-reasoning-effort"),
          api_key_env: textOrSkip("cascade-l2-api-key-env")
        },
        l3: {
          provider: C.value("cascade-l3-provider"),
          base_url: textOrSkip("cascade-l3-base-url"),
          model: textOrSkip("cascade-l3-model"),
          timeout: numOrSkip("cascade-l3-timeout"),
          max_tokens: numOrSkip("cascade-l3-max-tokens"),
          temperature: numOrSkip("cascade-l3-temperature"),
          json_mode: C.checked("cascade-l3-json-mode"),
          api_key_env: textOrSkip("cascade-l3-api-key-env"),
          api_key: C.readSecret(
            "cascade-l3-api-key",
            secrets.cascadeL3ApiKey
          )
        }
      }
    };
  }

  /* -------------------------------------------------------- 回填表单 */

  function fillForm(data) {
    var llm = data.llm || {};
    var routing = data.routing || {};
    var fallback = routing.fallback || {};
    var cascade = data.cascade || {};
    var l2 = cascade.l2 || {};
    var l3 = cascade.l3 || {};

    C.setValue("llm-provider", llm.provider);
    C.setValue("llm-base-url", llm.base_url);
    C.setValue("llm-model", llm.model);
    C.setValue("llm-timeout", llm.timeout);
    C.setValue("llm-max-context-chars", llm.max_context_chars);
    C.setValue("llm-risk-threshold", llm.risk_threshold);
    C.setValue("llm-keep-alive", llm.keep_alive);
    C.setValue("llm-failure-threshold", llm.failure_threshold);
    C.setValue("llm-breaker-open-seconds", llm.breaker_open_seconds);
    C.setValue("llm-temperature", llm.temperature);
    C.setValue("llm-max-tokens", llm.max_tokens);
    C.setChecked("llm-redact-before-llm", llm.redact_before_llm);
    C.setChecked("llm-json-mode", llm.json_mode);
    secrets.llmApiKey = C.resetSecret("llm-api-key", llm.api_key);

    C.setChecked("routing-enabled", routing.enabled);
    C.setChecked("routing-escalate", routing.escalate_on_red_line);
    C.setValue("routing-drift-threshold", routing.drift_escalate_threshold);
    C.setValue("fallback-provider", fallback.provider);
    C.setValue("fallback-base-url", fallback.base_url);
    C.setValue("fallback-model", fallback.model);
    secrets.fallbackApiKey = C.resetSecret(
      "fallback-api-key",
      fallback.api_key
    );

    // --- v3.5.0：级联审核 ---
    // ⚠️ v3.7.0：``cascade-enabled`` 已搬到「流水线控制」页。
    C.setChecked("cascade-require-agreement", cascade.require_agreement);
    C.setValue(
      "cascade-breaker-threshold",
      cascade.breaker_threshold
    );
    C.setValue(
      "cascade-breaker-open-seconds",
      cascade.breaker_open_seconds
    );

    C.setValue("cascade-l2-provider", l2.provider);
    C.setValue("cascade-l2-base-url", l2.base_url);
    C.setValue("cascade-l2-model", l2.model);
    C.setValue("cascade-l2-timeout", l2.timeout);
    C.setValue("cascade-l2-timeout-floor", l2.timeout_floor);
    C.setValue("cascade-l2-max-tokens", l2.max_tokens);
    C.setValue("cascade-l2-reasoning-effort", l2.reasoning_effort);
    C.setValue("cascade-l2-api-key-env", l2.api_key_env);

    C.setValue("cascade-l3-provider", l3.provider);
    C.setValue("cascade-l3-base-url", l3.base_url);
    C.setValue("cascade-l3-model", l3.model);
    C.setValue("cascade-l3-timeout", l3.timeout);
    C.setValue("cascade-l3-max-tokens", l3.max_tokens);
    C.setValue("cascade-l3-temperature", l3.temperature);
    C.setChecked("cascade-l3-json-mode", l3.json_mode);
    C.setValue("cascade-l3-api-key-env", l3.api_key_env);
    secrets.cascadeL3ApiKey = C.resetSecret(
      "cascade-l3-api-key",
      l3.api_key
    );
  }

  /* ------------------------------------------------------------ 启动 */

  function init() {
    C.bindCommonActions(collectPayload, fillForm);
    C.loadConfig().then(function (data) {
      if (data) {
        fillForm(data);
      }
    });
  }

  function destroy() {
    /* 本页没有定时器、没有 SSE —— DOM 被换掉就等于收摊了。 */
  }

  window.SentinelPage = { init: init, destroy: destroy };

  C.boot(init);
})();
