import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { OreClient } from '@ore/sdk';
import BrowserView, { browserFrameImageSource } from '../components/BrowserView';

const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF9kAAAAASUVORK5CYII=';
class FrameSocket {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor() { sockets.push(this); }
}
let sockets: FrameSocket[] = [];
let imageSources: string[] = [];
const drawImage = vi.fn();
beforeEach(() => {
  sockets = [];
  imageSources = [];
  drawImage.mockClear();
  vi.stubGlobal('WebSocket', FrameSocket);
  vi.stubGlobal('Image', class {
    onload: (() => void) | null = null;
    set src(value: string) { imageSources.push(value); this.onload?.(); }
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
});
afterEach(() => { vi.unstubAllGlobals(); });
function clientFor(transport: string) {
  return new OreClient({ baseUrl: 'https://ore.test', fetch: vi.fn(async () => new Response(JSON.stringify({
    sessions: [{ id: 'desktop-session', transport, title: 'Journal article', url: 'https://journal.test/article' }],
  }))) });
}

describe('native desktop browser view', () => {
  it.each([['raw PNG', png], ['PNG data URL', `data:image/png;base64,${png}`]])(
    'renders %s frames as an ORE desktop without requiring a companion', async (_name, data) => {
      render(<BrowserView client={clientFor('desktop_chrome')} profiles={[]}/>);
      expect(await screen.findByText('ORE virtual desktop · Chrome')).toBeInTheDocument();
      expect(screen.getByRole('note')).toHaveTextContent('Screenshot of Chrome and its toolbar');
      expect(screen.getByRole('note')).toHaveTextContent('Take control to click, type, paste, or scroll');
      expect(screen.queryByText(/install.*companion|pairing code/i)).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /companion|pairing/i })).not.toBeInTheDocument();
      await waitFor(() => expect(sockets).toHaveLength(1));
      act(() => {
        sockets[0].readyState = 1;
        sockets[0].onopen?.();
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'frame', data, width: 1280, height: 800,
          frame_id: 'desktop-frame', epoch: 4, control: 'agent' }) });
      });
      await waitFor(() => expect(imageSources).toContain(`data:image/png;base64,${png}`));
      const canvas = screen.getByLabelText('ORE virtual desktop screen');
      expect(canvas).toHaveAttribute('width', '1280');
      expect(canvas).toHaveAttribute('height', '800');
      expect(canvas).toHaveAttribute('tabindex', '-1');
      expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 1280, 800);
      expect(screen.getByText('1280 × 800 · Control epoch 4')).toBeInTheDocument();
    },
  );
  it('keeps a separate companion transport distinct from the native desktop', async () => {
    render(<BrowserView client={clientFor('chrome_companion')} profiles={[]}/>);
    expect(await screen.findByText('Journal article')).toBeInTheDocument();
    expect(screen.queryByText('ORE virtual desktop · Chrome')).not.toBeInTheDocument();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
});

describe('frame image formats', () => {
  it('uses an unambiguous PNG signature even if a legacy sender declares JPEG', () => {
    expect(browserFrameImageSource({ data: png, mime_type: 'image/jpeg' })).toBe(`data:image/png;base64,${png}`);
  });
  it('honors declared image MIME and keeps legacy JPEG fallback', () => {
    expect(browserFrameImageSource({ data: 'fixture', mime_type: 'image/webp' })).toBe('data:image/webp;base64,fixture');
    expect(browserFrameImageSource({ data: 'fixture', mime_type: 'image/png' })).toBe('data:image/png;base64,fixture');
    expect(browserFrameImageSource({ data: 'legacy-frame' })).toBe('data:image/jpeg;base64,legacy-frame');
    expect(browserFrameImageSource({ data: '/9j/fixture' })).toBe('data:image/jpeg;base64,/9j/fixture');
  });
  it('preserves an existing data URL instead of prepending a second MIME header', () => {
    const data = 'data:image/jpeg;base64,/9j/fixture';
    expect(browserFrameImageSource({ data, mime_type: 'image/png' })).toBe(data);
  });
});
