/* 哨兵 Sentinel —— 设置子页面：通知通道（v3.0.2）
 *
 * 只管这一页的字段：**channels**。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;
  var state = { channels: [] };

  /* 通道类型里带 webhook 的才需要地址 / 格式 / 密钥 / 超时 */
  function isWebhookChannel(type) {
    return String(type || "").indexOf("webhook") >= 0;
  }

  /* ------------------------------------------------------- 通道渲染 */

  function normalizeChannels(raw) {
    if (!Array.isArray(raw)) {
      return [];
    }
    return raw.map(function (item) {
      var source = item && typeof item === "object" ? item : {};
      var pending = "";
      if (source.secret !== undefined && source.secret !== null) {
        pending = String(source.secret);
      }
      return {
        raw: source,
        type: String(source.type || "webhook"),
        secretPending: pending,
        draft: {
          enabled: !!source.enabled,
          url: source.url === undefined || source.url === null
            ? ""
            : String(source.url),
          format: source.format ? String(source.format) : "generic",
          secret: "",
          timeout: source.timeout === undefined || source.timeout === null
            ? "10"
            : String(source.timeout)
        },
        fields: {}
      };
    });
  }

  function renderChannels() {
    var host = C.byId("channel-list");
    if (!host) {
      return;
    }
    host.innerHTML = "";
    if (!state.channels.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "暂无通知通道。";
      host.appendChild(empty);
      return;
    }
    state.channels.forEach(function (channel, index) {
      host.appendChild(buildChannelCard(channel, index));
    });
  }

  function buildChannelCard(channel, index) {
    var card = document.createElement("div");
    card.className = "settings-channel";

    var head = document.createElement("div");
    head.className = "settings-channel__head";

    var tag = document.createElement("span");
    tag.className = "chip chip--accent mono";
    tag.textContent = channel.type;
    head.appendChild(tag);

    var enabledBox = document.createElement("input");
    enabledBox.type = "checkbox";
    enabledBox.id = "ch-enabled-" + index;
    enabledBox.checked = !!channel.draft.enabled;
    channel.fields.enabled = enabledBox;
    head.appendChild(C.makeCheck(enabledBox, "启用"));

    if (isWebhookChannel(channel.type)) {
      var testButton = document.createElement("button");
      testButton.type = "button";
      testButton.className = "btn btn--ghost btn--sm";
      testButton.textContent = "测试";
      testButton.addEventListener("click", function () {
        testWebhook(index);
      });
      head.appendChild(testButton);
    }

    var removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "btn btn--ghost btn--sm";
    removeButton.textContent = "删除";
    removeButton.title = "删除后保存将不再包含该通道";
    removeButton.addEventListener("click", function () {
      removeChannel(index);
    });
    head.appendChild(removeButton);

    card.appendChild(head);

    if (isWebhookChannel(channel.type)) {
      var body = document.createElement("div");
      body.className = "settings-channel__body";

      var url = C.makeInput(
        "ch-url-" + index, "text", channel.draft.url, "地址"
      );
      body.appendChild(url.wrap);
      channel.fields.url = url.input;

      var format = C.makeSelect(
        "ch-format-" + index,
        ["generic", "dingtalk", "feishu", "bark", "serverchan"],
        channel.draft.format,
        "格式"
      );
      body.appendChild(format.wrap);
      channel.fields.format = format.input;

      var secret = C.makeInput(
        "ch-secret-" + index, "password", "", "密钥（留空表示不修改）"
      );
      body.appendChild(secret.wrap);
      channel.fields.secret = secret.input;
      C.markSecretState(secret.input, channel.secretPending);

      var timeout = C.makeInput(
        "ch-timeout-" + index, "number", channel.draft.timeout, "超时（秒）"
      );
      body.appendChild(timeout.wrap);
      channel.fields.timeout = timeout.input;

      card.appendChild(body);
    }

    var result = document.createElement("div");
    result.className = "settings-result";
    result.id = "ch-result-" + index;
    card.appendChild(result);

    return card;
  }

  /* 把用户正在填的内容收回草稿 —— 重绘列表前必须先做，
     否则「加一个通道」会把已填的地址和密钥冲掉。 */
  function syncChannelsFromDom() {
    state.channels.forEach(function (channel) {
      var fields = channel.fields || {};
      if (fields.enabled) {
        channel.draft.enabled = !!fields.enabled.checked;
      }
      if (fields.url) {
        channel.draft.url = fields.url.value;
      }
      if (fields.format) {
        channel.draft.format = fields.format.value;
      }
      if (fields.secret) {
        channel.draft.secret = fields.secret.value;
      }
      if (fields.timeout) {
        channel.draft.timeout = fields.timeout.value;
      }
    });
  }

  function addChannel() {
    syncChannelsFromDom();
    var select = C.byId("new-channel-type");
    var type = select ? String(select.value || "webhook") : "webhook";
    state.channels.push({
      raw: {
        type: type,
        enabled: false,
        url: "",
        format: "generic",
        secret: "",
        timeout: 10
      },
      type: type,
      secretPending: "",
      draft: {
        enabled: false,
        url: "",
        format: "generic",
        secret: "",
        timeout: "10"
      },
      fields: {}
    });
    renderChannels();
    C.setSaveStatus(
      "wait",
      "已添加一个「" + type + "」通道（默认不启用），记得点【保存并应用】。"
    );
  }

  function removeChannel(index) {
    syncChannelsFromDom();
    if (index < 0 || index >= state.channels.length) {
      return;
    }
    var removed = state.channels.splice(index, 1)[0];
    renderChannels();
    C.setSaveStatus(
      "wait",
      "已从页面移除「" + removed.type + "」通道。⚠️ 服务端是按序号写回的，" +
      "删中间一项会让后面的通道往前挪一位；真要删掉请手工改 config.yaml。"
    );
  }

  function channelPayload(channel) {
    var out = {};
    Object.keys(channel.raw || {}).forEach(function (key) {
      if (key !== "secret") {
        out[key] = channel.raw[key];
      }
    });
    out.type = channel.type;
    out.enabled = !!channel.draft.enabled;
    if (isWebhookChannel(channel.type)) {
      out.url = channel.draft.url || "";
      out.format = channel.draft.format || "generic";
      out.timeout = C.numOr(channel.draft.timeout, 10);
      out.secret = channel.draft.secret || channel.secretPending || "";
    } else if (channel.secretPending) {
      out.secret = channel.secretPending;
    }
    return out;
  }

  /* 测试通道 —— 真的发一条消息出去。
   * ⚠️ 密钥原样回传：打码值（********）由服务端换成配置里的真密钥，
   *    绝不能拿打码值当签名密钥发出去。 */
  function testWebhook(index) {
    syncChannelsFromDom();
    var channel = state.channels[index];
    if (!channel) {
      return;
    }
    var box = C.byId("ch-result-" + index);
    var url = String(channel.draft.url || "");
    if (!url) {
      C.showResult(box, "error", "请先填写 Webhook 地址。");
      return;
    }
    C.showResult(box, "wait", "测试中…（真的发一条消息出去）");
    var secret = channel.draft.secret || channel.secretPending || "";
    fetch("/api/webhook/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        format: channel.draft.format || "generic",
        secret: secret
      })
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          var detail = data.detail ? "：" + data.detail : "";
          C.showResult(box, "ok", "已送达" + detail);
        } else {
          var reason = (data && (data.error || data.detail)) || "未知原因";
          C.showResult(box, "error", "失败：" + reason);
        }
      })
      .catch(function (err) {
        C.showResult(box, "error", "测试失败：" + err.message);
      });
  }

  /* -------------------------------------------------------- 表单读取 */

  function collectPayload() {
    syncChannelsFromDom();
    return { channels: state.channels.map(channelPayload) };
  }

  function fillForm(data) {
    state.channels = normalizeChannels(data.channels);
    renderChannels();
  }

  /* ------------------------------------------------------------ 启动 */

  function init() {
    C.bindCommonActions(collectPayload, fillForm);
    var addButton = C.byId("add-channel-button");
    if (addButton) {
      addButton.addEventListener("click", addChannel);
    }
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
