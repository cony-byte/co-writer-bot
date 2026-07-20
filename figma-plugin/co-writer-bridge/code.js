// co-writer 스틸컷 브릿지 — 플러그인 메인 스레드.
// ui.html이 봇의 로컬 큐 서버(bot/figma_bridge.py)를 폴링해 대기 중인 스틸컷을 찾으면 이
// 스레드로 넘겨서 캔버스에 이미지 노드로 삽입하고(figma.* API는 UI iframe이 아니라 여기서만
// 쓸 수 있다), 반대로 사용자가 손본 노드를 선택해 "봇으로 보내기"를 누르면 PNG로 내보내
// ui.html에 돌려준다(실제 서버 전송은 ui.html이 fetch로 한다).

figma.showUI(__html__, { width: 320, height: 320 });

let cursorX = 0; // 새 스틸컷을 가로로 늘어놓기 위한 배치 커서 — 겹치지 않게

async function handleInsert(item) {
  try {
    const bytes = figma.base64Decode(item.image_b64);
    const image = figma.createImage(bytes);
    const { width, height } = await image.getSizeAsync();

    const node = figma.createRectangle();
    // 9:16 스틸컷이 보통 크기라 과하게 크면 화면을 다 차지하니 최대 480px 폭으로 스케일.
    const scale = Math.min(1, 480 / width);
    node.resize(width * scale, height * scale);
    node.x = cursorX;
    node.y = 0;
    node.fills = [{ type: "IMAGE", scaleMode: "FILL", imageHash: image.hash }];

    const work = item.work || "작품";
    const scene = item.scene_num != null ? `씬${item.scene_num}` : "";
    const cut = item.cut_num != null ? `컷${item.cut_num}` : "";
    node.name = `${work} ${scene} ${cut} (안전필터)`.trim();
    // ★되돌리기: 나중에 이 노드를 선택하고 "봇으로 보내기"를 누르면 어느 큐 항목인지 알아야
    // 하므로, 삽입 시점에 원래 item.id를 노드에 심어둔다(레이어 이름을 바꿔도 안 깨지게).
    node.setPluginData("coWriterItemId", item.id);

    figma.currentPage.appendChild(node);
    figma.viewport.scrollAndZoomIntoView([node]);
    cursorX += node.width + 40;

    figma.ui.postMessage({
      type: "inserted", id: item.id, scene_num: item.scene_num, cut_num: item.cut_num,
    });
  } catch (e) {
    figma.ui.postMessage({ type: "insert_failed", id: item.id, error: String(e) });
  }
}

async function handleSendBack() {
  const selection = figma.currentPage.selection;
  if (!selection.length) {
    figma.ui.postMessage({ type: "send_back_failed", error: "캔버스에서 손본 이미지를 먼저 선택해주세요." });
    return;
  }
  for (const node of selection) {
    const itemId = node.getPluginData("coWriterItemId");
    if (!itemId) {
      figma.ui.postMessage({ type: "send_back_failed", error: `"${node.name}"은 이 플러그인이 올린 이미지가 아니에요.` });
      continue;
    }
    try {
      const bytes = await node.exportAsync({ format: "PNG" });
      const image_b64 = figma.base64Encode(bytes);
      figma.ui.postMessage({ type: "send_back_ready", id: itemId, image_b64, node_name: node.name });
    } catch (e) {
      figma.ui.postMessage({ type: "send_back_failed", error: String(e) });
    }
  }
}

figma.ui.onmessage = (msg) => {
  if (msg.type === "insert") {
    handleInsert(msg.item);
  } else if (msg.type === "send_back") {
    handleSendBack();
  }
};
