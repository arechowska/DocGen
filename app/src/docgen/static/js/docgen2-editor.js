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

  const scrollToNode = (nodeId) => {
    if (!nodeId) return false;
    const canvas = document.querySelector("#docgen2DocumentCanvas");
    const target = canvas?.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
    if (!target) return false;
    document.querySelector('.mobile-tab[data-panel="docgen2Editor"]')?.click();
    target.scrollIntoView({behavior: "smooth", block: "center"});
    target.classList.remove("docgen-node-highlight");
    void target.offsetWidth;
    target.classList.add("docgen-node-highlight");
    setTimeout(() => target.classList.remove("docgen-node-highlight"), 1600);
    return true;
  };

  const nodeIdFromHash = (hash) => {
    const match = /^#doc-node-(.+)$/.exec(hash || "");
    return match ? decodeURIComponent(match[1]) : null;
  };

  const scrollToHashNode = () => {
    const hash = typeof window === "undefined" ? null : window.location?.hash;
    const nodeId = nodeIdFromHash(hash);
    if (nodeId) {
      scrollToNode(nodeId);
      return;
    }
    const panelId = /^#(sourcesPanel|docgen2Editor|chatPanel)$/.exec(hash || "")?.[1];
    if (!panelId) return;
    document.querySelector(`.mobile-tab[data-panel="${panelId}"]`)?.click();
    document.querySelector(`#${CSS.escape(panelId)}`)?.scrollIntoView?.({block: "start"});
  };

  document.addEventListener("click", (event) => {
    const dismiss = event.target.closest("[data-dismiss-fix-preview]");
    if (dismiss) {
      dismiss.closest("section")?.remove();
      return;
    }
    const link = event.target.closest("[data-node-target]");
    if (!link) return;
    if (scrollToNode(link.dataset.nodeTarget)) event.preventDefault();
  });
  document.addEventListener("htmx:afterSwap", scrollToHashNode);
  if (typeof window !== "undefined") window.addEventListener?.("hashchange", scrollToHashNode);
  scrollToHashNode();

  const templateSource = document.querySelector("[data-template-source]");
  const templateStorageKey = templateSource?.dataset?.templateStorageKey;
  if (templateSource && templateStorageKey) {
    try {
      const storedTemplate = window.localStorage.getItem(templateStorageKey);
      const storedTemplateExists = Array.from(templateSource.options).some(
        (option) => option.value === storedTemplate,
      );
      if (storedTemplate && storedTemplateExists) templateSource.value = storedTemplate;
    } catch (_) {
      // Storage can be unavailable in privacy modes; server state remains usable.
    }
  }
  const synchronizeConversion = () => {
    const buildButton = document.querySelector("#buildButton");
    const formatSource = document.querySelector("#formatSelect");
    const formattingSource = document.querySelector(
      "#export-template-select select[name='template_id']",
    );
    const conversionFormat = document.querySelector("[data-conversion-format]");
    const conversionTemplate = document.querySelector("[data-conversion-template]");
    const importProfile = document.querySelector("[data-editor-import-profile]");
    const withoutTemplate = templateSource?.value === "no-template";
    if (conversionFormat) conversionFormat.value = formatSource?.value || "";
    if (conversionTemplate) {
      conversionTemplate.value = formattingSource?.disabled ? "" : formattingSource?.value || "";
    }
    const htmlWithoutTemplate = withoutTemplate && formatSource?.value === "html";
    if (importProfile) importProfile.value = htmlWithoutTemplate ? "no-template-html" : "";
    if (!buildButton) return;
    const hasDocument = buildButton.dataset.hasDocument === "true";
    const buildForm = htmlWithoutTemplate
      ? (hasDocument ? "export-form" : "editorImportForm")
      : (withoutTemplate ? "conversionForm" : "assembleForm");
    buildButton.setAttribute("form", buildForm);
    const sourceAvailable = buildButton.dataset.sourceAvailable === "true";
    const conversionReady = Boolean(formatSource?.value && conversionTemplate?.value);
    const needsSource = !(htmlWithoutTemplate && hasDocument);
    buildButton.disabled =
      (needsSource && !sourceAvailable) ||
      (withoutTemplate && !conversionReady);
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
    templateSource.addEventListener("change", () => {
      if (templateStorageKey) {
        try {
          window.localStorage.setItem(templateStorageKey, templateSource.value);
        } catch (_) {
          // Keep the selected value for this page even when storage is unavailable.
        }
      }
      synchronizeTemplate();
    });
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
  let savedSelection = null;

  const focusCanvas = () => {
    canvas.focus();
  };

  const selectionBelongsToCanvas = (range) => {
    const container = range?.commonAncestorContainer;
    if (!container) return false;
    const element = container.nodeType === Node.ELEMENT_NODE ? container : container.parentElement;
    return element === canvas || canvas.contains(element);
  };

  const rememberSelection = () => {
    const selection = window.getSelection();
    if (!selection?.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (selectionBelongsToCanvas(range)) savedSelection = range.cloneRange();
  };

  const restoreSelection = () => {
    focusCanvas();
    if (!savedSelection || !canvas.contains(savedSelection.startContainer)) return;
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(savedSelection);
  };

  const normalizeSemanticNodeAttributes = () => {
    const seenNodeIds = new Set();
    const semanticAttributes = [
      "data-node-id",
      "data-kind",
      "data-section-id",
      "data-section-title",
    ];
    canvas.querySelectorAll("[data-node-id]").forEach((element) => {
      const nodeId = element.dataset.nodeId;
      if (!nodeId || !seenNodeIds.has(nodeId)) {
        if (nodeId) seenNodeIds.add(nodeId);
        return;
      }
      // formatBlock may split a block and copy its semantic identity. The
      // additional fragment must be saved as a new manual node instead.
      semanticAttributes.forEach((attribute) => element.removeAttribute(attribute));
    });
  };

  const runCommand = (command, value = null) => {
    restoreSelection();
    document.execCommand(command, false, value);
    normalizeSemanticNodeAttributes();
    rememberSelection();
  };

  const canvasBlockForNode = (node) => {
    let element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    while (element && element.parentElement !== canvas) element = element.parentElement;
    return element?.parentElement === canvas ? element : null;
  };

  const selectedCanvasBlocks = () => {
    restoreSelection();
    const selection = window.getSelection();
    if (!selection?.rangeCount) return [];
    const range = selection.getRangeAt(0);
    if (range.collapsed) {
      const block = canvasBlockForNode(range.startContainer);
      return block ? [block] : [];
    }
    return Array.from(canvas.children).filter((element) => {
      try {
        return range.intersectsNode(element);
      } catch (_) {
        return false;
      }
    });
  };

  const placeCaretAtEnd = (element) => {
    if (!element) return;
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    savedSelection = range.cloneRange();
  };

  const replaceBlockTag = (block, tagName) => {
    if (block.tagName.toLowerCase() === tagName) return block;
    const replacement = document.createElement(tagName);
    Array.from(block.attributes).forEach((attribute) => {
      replacement.setAttribute(attribute.name, attribute.value);
    });
    replacement.innerHTML = block.innerHTML;
    replacement.dataset.kind = tagName === "p" ? "paragraph" : "heading";
    block.replaceWith(replacement);
    return replacement;
  };

  const applyHeadingStyle = () => {
    const tagName = headingSelect?.value;
    if (!tagName) return;
    const supportedBlock = /^(P|DIV|H[1-6])$/;
    const replacements = selectedCanvasBlocks()
      .filter((block) => supportedBlock.test(block.tagName))
      .map((block) => replaceBlockTag(block, tagName));
    normalizeSemanticNodeAttributes();
    if (replacements.length) placeCaretAtEnd(replacements.at(-1));
    headingSelect.value = "";
  };

  const copySemanticAttributes = (source, target) => {
    ["data-node-id", "data-section-id", "data-section-title"].forEach((attribute) => {
      const value = source.getAttribute(attribute);
      if (value !== null) target.setAttribute(attribute, value);
    });
  };

  const unwrapList = (list) => {
    const paragraphs = Array.from(list.children).map((item, index) => {
      const paragraph = document.createElement("p");
      paragraph.innerHTML = item.innerHTML || "<br>";
      if (index === 0) copySemanticAttributes(list, paragraph);
      paragraph.dataset.kind = "paragraph";
      return paragraph;
    });
    if (!paragraphs.length) paragraphs.push(document.createElement("p"));
    list.replaceWith(...paragraphs);
    return paragraphs;
  };

  const toggleList = (tagName) => {
    const blocks = selectedCanvasBlocks();
    if (!blocks.length) return;
    if (blocks.length === 1 && blocks[0].tagName.toLowerCase() === tagName) {
      const paragraphs = unwrapList(blocks[0]);
      normalizeSemanticNodeAttributes();
      if (paragraphs.length) placeCaretAtEnd(paragraphs.at(-1));
      return;
    }

    const list = document.createElement(tagName);
    copySemanticAttributes(blocks[0], list);
    list.dataset.kind = "list";
    blocks[0].before(list);
    blocks.forEach((block) => {
      if (/^(UL|OL)$/.test(block.tagName)) {
        Array.from(block.children).forEach((item) => list.appendChild(item));
      } else {
        const item = document.createElement("li");
        item.innerHTML = block.innerHTML;
        if (block.getAttribute("style")) item.setAttribute("style", block.getAttribute("style"));
        list.appendChild(item);
      }
      block.remove();
    });
    normalizeSemanticNodeAttributes();
    placeCaretAtEnd(list);
  };

  const applyAlignment = (alignment) => {
    const blocks = selectedCanvasBlocks();
    blocks.forEach((block) => {
      block.style.textAlign = alignment;
    });
    rememberSelection();
  };

  const clearFormatting = () => {
    const clearedBlocks = [];
    selectedCanvasBlocks().forEach((block) => {
      if (/^(UL|OL)$/.test(block.tagName)) {
        clearedBlocks.push(...unwrapList(block));
        return;
      }
      const cleared = /^(H[1-6]|DIV)$/.test(block.tagName)
        ? replaceBlockTag(block, "p")
        : block;
      clearedBlocks.push(cleared);
    });
    clearedBlocks.forEach((block) => {
      block.removeAttribute("style");
      block.querySelectorAll("[style]").forEach((element) => element.removeAttribute("style"));
      block.querySelectorAll("b, strong, i, em, u, s, font, a").forEach((element) => {
        element.replaceWith(...element.childNodes);
      });
    });
    normalizeSemanticNodeAttributes();
    if (headingSelect) headingSelect.value = "";
    if (clearedBlocks.length) placeCaretAtEnd(clearedBlocks.at(-1));
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

  const editorSaveErrorDetail = (result) => {
    const detail = result?.detail;
    if (typeof detail === "string") return detail;
    if (!Array.isArray(detail)) return "Не удалось сохранить";
    const issues = detail.filter((item) => item && typeof item === "object");
    if (issues.some((item) => item.loc?.includes?.("html") && item.type === "string_too_long")) {
      return "Документ слишком большой для сохранения";
    }
    if (issues.some((item) => item.loc?.includes?.("title"))) {
      return "Проверь название документа";
    }
    if (issues.some((item) => item.loc?.includes?.("revision"))) {
      return "Не удалось определить версию документа. Обнови страницу и повтори сохранение";
    }
    return "Не удалось сохранить: проверь содержимое документа";
  };

  const saveWorkspace = async () => {
    const saveUrl = editor.dataset.saveUrl;
    if (!saveUrl || !saveButton) return null;
    const creatingDocument = !Number.isInteger(
      Number.parseInt(editor.dataset.revision, 10),
    );
    normalizeSemanticNodeAttributes();
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
        const detail = editorSaveErrorDetail(result);
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
      const noTemplateHtml =
        templateSource?.value === "no-template" &&
        document.querySelector("#formatSelect")?.value === "html";
      if (creatingDocument && !noTemplateHtml && typeof window.location?.reload === "function") {
        window.location.reload();
      }
      return result;
    } catch (error) {
      const message = error instanceof Error ? error.message : "Не удалось сохранить";
      setSaveStatus(message, "error");
      return null;
    } finally {
      saveButton.disabled = false;
    }
  };

  editor.docgenSaveWorkspace = saveWorkspace;

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
    rememberSelection();
    contextCell();
  });

  canvas.addEventListener("mouseup", () => {
    rememberSelection();
    contextCell();
  });

  editor.addEventListener("mousedown", (event) => {
    const button = event.target.closest(
      "button[data-editor-command], button[data-editor-clear-formatting]",
    );
    if (!button) return;
    rememberSelection();
    // Keep the caret/range in contenteditable until execCommand runs on click.
    event.preventDefault();
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
    if (event.target.closest("[data-editor-clear-formatting]")) {
      clearFormatting();
      return;
    }
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
    if (command === "insertUnorderedList" || command === "insertOrderedList") {
      toggleList(command === "insertOrderedList" ? "ol" : "ul");
      return;
    }
    if (command === "justifyLeft" || command === "justifyCenter" || command === "justifyRight") {
      const alignment = command.replace("justify", "").toLowerCase();
      applyAlignment(alignment);
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
    applyHeadingStyle();
  });

  imageInput?.addEventListener("change", () => {
    insertImage(imageInput.files?.[0]);
    imageInput.value = "";
  });

  saveButton?.addEventListener("click", () => {
    return saveWorkspace();
  });
  };

  document.addEventListener("submit", async (event) => {
    const buildButton = event.submitter;
    if (event.target?.id !== "export-form" || buildButton?.id !== "buildButton") return;
    const noTemplateHtml =
      document.querySelector("[data-template-source]")?.value === "no-template" &&
      document.querySelector("#formatSelect")?.value === "html";
    if (!noTemplateHtml) return;
    event.preventDefault();
    const editor = document.querySelector("#docgen2Editor");
    const saved = await editor?.docgenSaveWorkspace?.();
    if (!saved) return;
    window.htmx?.trigger?.(
      document.body,
      "docgen:html-build",
      {revision: saved.revision},
    );
  });

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
