(() => {
  document.querySelectorAll("form[data-confirm-delete]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const message = form.dataset.confirmMessage || "Удалить проект?";
      if (!window.confirm(message)) event.preventDefault();
    });
  });

  document.querySelectorAll(".mobile-tab[data-panel]").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".mobile-tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".workspace > .panel").forEach((panel) => {
        panel.classList.remove("mobile-active");
      });
      tab.classList.add("active");
      document.querySelector(`#${CSS.escape(tab.dataset.panel)}`)?.classList.add("mobile-active");
    });
  });

  const templateSource = document.querySelector("[data-template-source]");
  const synchronizeConversion = () => {
    const buildButton = document.querySelector("#buildButton");
    const formatSource = document.querySelector("#formatSelect");
    const formattingSource = document.querySelector(
      "#export-template-select select[name='template_id']",
    );
    const conversionFormat = document.querySelector("[data-conversion-format]");
    const conversionTemplate = document.querySelector("[data-conversion-template]");
    const withoutTemplate = templateSource?.value === "no-template";
    if (conversionFormat) conversionFormat.value = formatSource?.value || "";
    if (conversionTemplate) {
      conversionTemplate.value = formattingSource?.disabled ? "" : formattingSource?.value || "";
    }
    if (!buildButton) return;
    buildButton.setAttribute("form", withoutTemplate ? "conversionForm" : "assembleForm");
    const label = buildButton.querySelector("[data-build-label]");
    if (label) label.textContent = withoutTemplate ? "Открыть" : "Собрать";
    const sourceAvailable = buildButton.dataset.sourceAvailable === "true";
    const conversionReady = Boolean(formatSource?.value && conversionTemplate?.value);
    buildButton.disabled = !sourceAvailable || (withoutTemplate && !conversionReady);
  };
  const synchronizeTemplate = () => {
    document.querySelectorAll("[data-template-target]").forEach((target) => {
      target.value = templateSource?.value || "";
    });
    const reviewButton = document.querySelector("[data-template-required]");
    if (reviewButton) {
      reviewButton.disabled =
        templateSource?.value === "no-template" ||
        reviewButton.dataset.checkAvailable !== "true";
    }
    synchronizeConversion();
  };

  if (templateSource) {
    synchronizeTemplate();
    templateSource.addEventListener("change", synchronizeTemplate);
  }
  document.addEventListener("change", (event) => {
    if (event.target?.matches?.("#formatSelect, #export-template-select select")) {
      synchronizeConversion();
    }
  });
  document.addEventListener("htmx:afterSwap", synchronizeConversion);

  const applySanitizedInlineStyles = (root) => {
    root?.querySelectorAll?.("[style]").forEach((element) => {
      const rawStyle = element.getAttribute("style");
      if (!rawStyle) return;
      element.removeAttribute("style");
      rawStyle.split(";").forEach((declaration) => {
        const separator = declaration.indexOf(":");
        if (separator < 0) return;
        const property = declaration.slice(0, separator).trim();
        const value = declaration.slice(separator + 1).trim();
        if (property && value) element.style.setProperty(property, value);
      });
    });
  };

  const initializeChat = () => {
    const form = document.querySelector("#chatForm");
    if (!form || form.dataset.chatInitialized === "true") return;
    const input = form.querySelector("#chatInput");
    const revision = form.querySelector('input[name="revision"]');
    const button = form.querySelector("#sendButton");
    const errorBanner = document.querySelector("#errorBanner");
    const statusBadge = document.querySelector("#statusBadge");
    const statusText = document.querySelector("#statusText");
    if (!input || !revision || !button) return;

    form.dataset.chatInitialized = "true";
    form.addEventListener("submit", (event) => {
      if (!window.htmx?.ajax) return;
      event.preventDefault();
      const message = input.value.trim();
      if (!message || !revision.value) {
        if (errorBanner) {
          errorBanner.textContent = "Введите сообщение и повторите попытку";
          errorBanner.hidden = false;
        }
        return;
      }

      if (errorBanner) errorBanner.hidden = true;
      button.disabled = true;
      const userMessage = document.createElement("div");
      userMessage.className = "message chat-user-message";
      userMessage.textContent = message;
      document.querySelector("#chat-messages")?.appendChild(userMessage);
      const thinkingMessage = document.createElement("div");
      thinkingMessage.className = "message chat-thinking-message";
      thinkingMessage.textContent = "Думаю…";
      document.querySelector("#chat-messages")?.appendChild(thinkingMessage);
      statusBadge?.setAttribute("data-state", "thinking");
      if (statusText) statusText.textContent = "Думаю…";
      input.value = "";
      let request;
      try {
        request = window.htmx.ajax("POST", form.action, {
          source: form,
          target: "#chat-messages",
          swap: "beforeend",
          values: {message, revision: revision.value},
          timeout: 30000,
        });
      } catch (error) {
        request = Promise.reject(error);
      }
      Promise.resolve(request)
        .catch(() => {
          if (!input.value) input.value = message;
          if (errorBanner) {
            errorBanner.textContent = "Не удалось отправить сообщение";
            errorBanner.hidden = false;
          }
        })
        .finally(() => {
          thinkingMessage.remove?.();
          statusBadge?.setAttribute("data-state", "ready");
          if (statusText) statusText.textContent = "Готово";
          button.disabled = false;
        });
    });
  };

  const initializeEditor = () => {
    const editor = document.querySelector("#docgen2Editor");
    if (!editor || editor.dataset.editorInitialized === "true") return;

    const canvas = editor.querySelector("#docgen2DocumentCanvas");
  const titleInput = editor.querySelector("#docgen2EditorTitle");
  const headingSelect = editor.querySelector("[data-editor-heading]");
  const imageInput = editor.querySelector("#docgen2ImageInput");
  const tableButton = editor.querySelector("[data-editor-command=\"table\"]");
  const tableMenu = editor.querySelector("#tableMenu");
  const tableRows = editor.querySelector("[data-table-rows]");
  const tableColumns = editor.querySelector("[data-table-columns]");
  const saveButton = editor.querySelector("[data-editor-save]");
    const saveStatus = editor.querySelector("[data-editor-save-status]");
    if (!canvas) return;
    applySanitizedInlineStyles(canvas);
    editor.dataset.editorInitialized = "true";
  let lastTableContext = null;

  const focusCanvas = () => {
    canvas.focus();
  };

  const runCommand = (command, value = null) => {
    focusCanvas();
    document.execCommand(command, false, value);
  };

  let saveStatusTimer = null;

  const setSaveStatus = (message, state = "") => {
    if (!saveStatus) return;
    if (saveStatusTimer) clearTimeout(saveStatusTimer);
    const isSaved = state === "saved";
    saveStatus.textContent = isSaved ? "✓" : message;
    saveStatus.title = isSaved ? message : "";
    saveStatus.ariaLabel = isSaved ? message : "";
    saveStatus.dataset.state = state;
    if (isSaved) {
      saveStatusTimer = setTimeout(() => setSaveStatus(""), 2000);
    }
  };

  const markDocumentReady = () => {
    const statusBadge = document.querySelector("#statusBadge");
    const statusText = document.querySelector("#statusText");
    const chatInput = document.querySelector("#chatInput");
    const sendButton = document.querySelector("#sendButton");
    statusBadge?.setAttribute("data-state", "ready");
    if (statusText) statusText.textContent = "Готово";
    chatInput?.removeAttribute("disabled");
    sendButton?.removeAttribute("disabled");
  };

  const saveWorkspace = async () => {
    const saveUrl = editor.dataset.saveUrl;
    if (!saveUrl || !saveButton) return;
    saveButton.disabled = true;
    setSaveStatus("Сохранение...", "pending");
    try {
      const response = await fetch(saveUrl, {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          title: titleInput?.value || "Новый документ",
          html: canvas.innerHTML,
          revision: Number.parseInt(editor.dataset.revision, 10),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof result.detail === "string" ? result.detail : "Не удалось сохранить";
        if (response.status === 409) {
          throw new Error(`${detail}. Обнови страницу и повтори сохранение.`);
        }
        throw new Error(detail);
      }
      editor.dataset.revision = String(result.revision);
      canvas.innerHTML = result.html;
      applySanitizedInlineStyles(canvas);
      document.querySelectorAll('input[name="revision"]').forEach((input) => {
        input.value = String(result.revision);
      });
      window.htmx?.trigger?.(
        document.body,
        "docgen:document-updated",
        {revision: result.revision},
      );
      markDocumentReady();
      setSaveStatus("Сохранено в проекте", "saved");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Не удалось сохранить";
      setSaveStatus(message, "error");
    } finally {
      saveButton.disabled = false;
    }
  };

  const closeTableMenu = () => {
    if (!tableMenu || !tableButton) return;
    tableMenu.hidden = true;
    tableButton.setAttribute("aria-expanded", "false");
  };

  const toggleTableMenu = () => {
    if (!tableMenu || !tableButton) return;
    tableMenu.hidden = !tableMenu.hidden;
    tableButton.setAttribute("aria-expanded", tableMenu.hidden ? "false" : "true");
  };

  const clampSize = (value) => {
    const number = Number.parseInt(value, 10);
    if (Number.isNaN(number)) return 3;
    return Math.min(6, Math.max(1, number));
  };

  const tableCellHtml = (tag) => `<${tag}><br></${tag}>`;

  const insertTable = (rows = 3, columns = 3) => {
    const safeRows = clampSize(rows);
    const safeColumns = clampSize(columns);
    const body = Array.from({ length: safeRows }, (_, rowIndex) => {
      const tag = rowIndex === 0 ? "th" : "td";
      const cells = Array.from({ length: safeColumns }, () => tableCellHtml(tag)).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    runCommand("insertHTML", `<table><tbody>${body}</tbody></table><p></p>`);
  };

  const rememberTableContext = (cell) => {
    const table = cell?.closest("table") || null;
    const row = cell?.closest("tr") || null;
    if (!table || !row || !cell) return null;
    lastTableContext = {
      table,
      row,
      rowIndex: Array.from(table.querySelectorAll("tr")).indexOf(row),
      cellIndex: cell.cellIndex,
    };
    return lastTableContext;
  };

  const contextCell = () => {
    const selection = window.getSelection();
    const node = selection?.anchorNode;
    const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    const selectedCell = element?.closest("td,th") || null;
    if (selectedCell) return rememberTableContext(selectedCell)?.row.children[selectedCell.cellIndex] || null;
    const table = lastTableContext?.table;
    if (!table?.isConnected) return null;
    const row =
      lastTableContext.row?.isConnected
        ? lastTableContext.row
        : table.querySelectorAll("tr")[Math.min(lastTableContext.rowIndex, table.querySelectorAll("tr").length - 1)];
    return row?.children[Math.min(lastTableContext.cellIndex, row.children.length - 1)] || null;
  };

  const currentRow = () => contextCell()?.closest("tr") || null;

  const currentTable = () => contextCell()?.closest("table") || null;

  const addTableRow = () => {
    const row = currentRow();
    if (!row) return;
    const clone = row.cloneNode(true);
    clone.querySelectorAll("td,th").forEach((cell) => {
      cell.innerHTML = "<br>";
    });
    row.after(clone);
  };

  const deleteTableRow = () => {
    const row = currentRow();
    const table = currentTable();
    if (!row || !table) return;
    if (table.querySelectorAll("tr").length <= 1) {
      table.remove();
      return;
    }
    row.remove();
  };

  const addTableColumn = () => {
    const cell = contextCell();
    const table = currentTable();
    if (!cell || !table) return;
    const index = cell.cellIndex;
    table.querySelectorAll("tr").forEach((row) => {
      const reference = row.children[index];
      const tag = reference?.tagName.toLowerCase() === "th" ? "th" : "td";
      const newCell = document.createElement(tag);
      newCell.innerHTML = "<br>";
      reference?.after(newCell);
    });
  };

  const deleteTableColumn = () => {
    const cell = contextCell();
    const table = currentTable();
    if (!cell || !table) return;
    const index = cell.cellIndex;
    const firstRow = table.querySelector("tr");
    if ((firstRow?.children.length || 0) <= 1) {
      table.remove();
      return;
    }
    table.querySelectorAll("tr").forEach((row) => {
      row.children[index]?.remove();
    });
  };

  canvas.addEventListener("keyup", () => {
    contextCell();
  });

  canvas.addEventListener("mouseup", () => {
    contextCell();
  });

  const insertImage = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      runCommand("insertImage", String(reader.result));
    });
    reader.readAsDataURL(file);
  };

  editor.addEventListener("click", (event) => {
    const button = event.target.closest("[data-editor-command]");
    if (!button) return;
    const command = button.dataset.editorCommand;
    if (command === "link") {
      const selectedText = String(window.getSelection() || "");
      const url = window.prompt("Адрес ссылки", selectedText.startsWith("http") ? selectedText : "");
      if (url) runCommand("createLink", url);
      return;
    }
    if (command === "image") {
      imageInput?.click();
      return;
    }
    if (command === "table") {
      toggleTableMenu();
      return;
    }
    runCommand(command);
  });

  tableMenu?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-table-action]");
    if (!button) return;
    event.preventDefault();
    const action = button.dataset.tableAction;
    if (action === "insert") insertTable(tableRows?.value, tableColumns?.value);
    if (action === "add-row") addTableRow();
    if (action === "delete-row") deleteTableRow();
    if (action === "add-column") addTableColumn();
    if (action === "delete-column") deleteTableColumn();
    if (action === "insert") closeTableMenu();
  });

  headingSelect?.addEventListener("change", () => {
    runCommand("formatBlock", headingSelect.value || "p");
  });

  imageInput?.addEventListener("change", () => {
    insertImage(imageInput.files?.[0]);
    imageInput.value = "";
  });

  saveButton?.addEventListener("click", () => {
    return saveWorkspace();
  });
  };

  document.addEventListener("click", (event) => {
    const activeEditor = document.querySelector("#docgen2Editor");
    const activeMenu = activeEditor?.querySelector("#tableMenu");
    if (!activeMenu || activeMenu.hidden || activeEditor.contains(event.target)) return;
    activeMenu.hidden = true;
    activeEditor
      .querySelector('[data-editor-command="table"]')
      ?.setAttribute("aria-expanded", "false");
  });
  document.addEventListener("htmx:afterSwap", () => {
    synchronizeTemplate();
    initializeEditor();
    initializeChat();
  });
  document.addEventListener("docgen:document-updated", (event) => {
    const revision = event.detail?.revision;
    if (!Number.isInteger(revision)) return;
    document.querySelectorAll('input[name="revision"]').forEach((input) => {
      input.value = String(revision);
    });
  });
  initializeChat();
  initializeEditor();
})();
