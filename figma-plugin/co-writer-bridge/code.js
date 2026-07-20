// co-writer 스틸컷 브릿지 — 플러그인 메인 스레드.
// ui.html이 봇의 로컬 큐 서버(bot/figma_bridge.py)를 폴링해 대기 중인 스틸컷을 찾으면,
// 이 스레드로 넘겨서 실제 캔버스에 이미지 노드로 삽입한다(figma.* API는 UI iframe이 아니라
// 여기서만 쓸 수 있다).

figma.showUI(__html__, { width: 320, height: 280 });

let cursorX = 0; // 새 스틸컷을 가로로 늘어놓기 위한 배치 커서 — 겹치지 않게

function base64ToUint8Array(base64) {
  const binary = figma.base64Decode(base64);
  return binary;
}

figma.ui.onmessage = async (msg) => {
  if (msg.type !== "insert") return;
  const item = msg.item;
  try {
    const bytes = base64ToUint8Array(item.image_b64);
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

    figma.currentPage.appendChild(node);
    figma.viewport.scrollAndZoomIntoView([node]);
    cursorX += node.width + 40;

    figma.ui.postMessage({
      type: "inserted", id: item.id, scene_num: item.scene_num, cut_num: item.cut_num,
    });
  } catch (e) {
    figma.ui.postMessage({ type: "insert_failed", id: item.id, error: String(e) });
  }
};
