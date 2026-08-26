import "@testing-library/jest-dom/vitest";

const originalGetComputedStyle = window.getComputedStyle.bind(window);

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

window.getComputedStyle = ((element: Element) => originalGetComputedStyle(element)) as typeof window.getComputedStyle;
