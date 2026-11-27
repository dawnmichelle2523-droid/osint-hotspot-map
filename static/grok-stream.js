(function () {
  async function post(url, body, handlers) {
    handlers = handlers || {};
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(body || {})
      });
    } catch (err) {
      if (handlers.onError) handlers.onError({ message: "Grok could not be reached." });
      return;
    }
    if (!response.ok || !response.body) {
      let message = "Grok request failed.";
      try {
        const data = await response.json();
        if (data.message) message = data.message;
      } catch (err) {}
      if (handlers.onError) handlers.onError({ message: message });
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    function parseBlock(block) {
      let event = "message";
      const data = [];
      block.split("\n").forEach((line) => {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      });
      if (!data.length) return;
      let payload = {};
      try { payload = JSON.parse(data.join("")); } catch (err) { payload = { text: data.join("") }; }
      if (event === "token" && handlers.onToken) handlers.onToken(payload.text || "");
      else if (event === "done" && handlers.onDone) handlers.onDone(payload);
      else if (event === "error" && handlers.onError) handlers.onError(payload);
    }
    while (true) {
      const step = await reader.read();
      if (step.done) break;
      buffer += decoder.decode(step.value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      parts.forEach(parseBlock);
    }
    if (buffer.trim()) parseBlock(buffer);
  }
  window.GeoRiskStream = { post: post };
})();
