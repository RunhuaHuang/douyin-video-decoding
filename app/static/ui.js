/* 抖音文案提取 · 共享交互组件层（纯 vanilla，无依赖，挂 window.UI） */
(function () {
  "use strict";
  /* ---------- Toast ---------- */
  var TOAST_DURATION = { success: 3500, error: 5000, info: 3500 };

  function toastStack() {
    var stack = document.getElementById("toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "toast-stack";
      document.body.appendChild(stack);
    }
    return stack;
  }

  function toast(message, kind) {
    kind = Object.prototype.hasOwnProperty.call(TOAST_DURATION, kind) ? kind : "info";
    var el = document.createElement("div");
    el.className = "toast toast-" + kind;
    el.setAttribute("role", "status");
    var text = document.createElement("span");
    text.textContent = String(message); // textContent 防注入
    el.appendChild(text);
    var hideTimer = null;
    function dismiss() {
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = null;
      }
      if (!el.parentNode) return;
      el.classList.add("is-leaving");
      setTimeout(function () {
        el.remove();
      }, 200);
    }
    function schedule(delay) {
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(dismiss, delay);
    }
    el.addEventListener("click", dismiss);
    el.addEventListener("mouseenter", function () {
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = null;
      }
    });
    el.addEventListener("mouseleave", function () {
      schedule(800); // 移出后给短宽限再消失
    });
    toastStack().appendChild(el);
    schedule(TOAST_DURATION[kind]);
    return el;
  }

  /* ---------- HTML 转义：轮询路径 innerHTML 拼接服务端数据前必须过一遍 ---------- */
  var ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return ESCAPE_MAP[ch];
    });
  }

  /* ---------- 两段式确认 ---------- */
  var armed = new WeakMap(); // button -> { original, onConfirm, timer }

  function disarm(button) {
    var state = armed.get(button);
    if (!state) return;
    clearTimeout(state.timer);
    armed.delete(button);
    button.classList.remove("is-armed");
    button.textContent = state.original;
  }

  function confirm(button, onConfirm) {
    if (typeof onConfirm !== "function") return;
    if (armed.has(button)) {
      disarm(button);
      // 第二次点击：按钮已被禁用（is-disabled）则忽略
      if (button.classList.contains("is-disabled") || button.disabled) return;
      onConfirm.call(button);
      // 防重入：onConfirm 触发后立即禁用，连点不再重复执行；完成或失败后由调用方
      // 刷新页面/重渲染表格重建按钮，is-disabled 随之自然复位，无需在这里处理
      button.classList.add("is-disabled");
      return;
    }
    var timer = setTimeout(function () {
      disarm(button);
    }, 3000);
    armed.set(button, { original: button.textContent, onConfirm: onConfirm, timer: timer });
    button.classList.add("is-armed");
    button.textContent = "确认" + button.textContent + "？";
  }

  /* ---------- 复制到剪贴板 ---------- */
  function copy(text) {
    function done() {
      toast("已复制", "success");
    }
    function fail() {
      toast("复制失败", "error");
    }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = String(text);
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      try {
        if (document.execCommand("copy")) done();
        else fail();
      } catch (error) {
        fail();
      }
      ta.remove();
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(String(text)).then(done, fallback);
    } else {
      fallback();
    }
  }

  /* ---------- 日志跟随滚动 ---------- */
  function followScroll(el) {
    if (!el.dataset.followBound) {
      el.dataset.followBound = "1";
      el._followPinned = true; // 用户上翻后置 false，回到底部恢复 true
      el.addEventListener("scroll", function () {
        el._followPinned = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      });
    }
    if (el._followPinned) el.scrollTop = el.scrollHeight;
  }

  /* ---------- 状态中文化（纯显示层） ---------- */
  var STATUS_LABELS = {
    completed: "已完成",
    failed: "失败",
    running: "运行中",
    paused: "已暂停",
    queued: "排队中",
    uploaded: "待配置",
  };

  function statusLabel(status) {
    if (status == null) return status;
    return STATUS_LABELS[status] || status;
  }

  window.UI = {
    toast: toast,
    confirm: confirm,
    copy: copy,
    esc: esc,
    followScroll: followScroll,
    statusLabel: statusLabel,
  };
})();
