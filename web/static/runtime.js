// Shared SPA lifecycle state. Page modules depend on this instead of window globals.
import { setRouteAbort } from './helpers.js';

let routeToken = 0;
let routeAbort = null;
let routeEventSource = null;
let errorRenderer = () => {};
const routeTimeouts = new Set();
const routeIntervals = new Set();

export function closeRouteResources() {
  if (routeEventSource) {
    routeEventSource.close();
    routeEventSource = null;
  }
  if (routeAbort) {
    routeAbort.abort();
    routeAbort = null;
  }
  setRouteAbort(null);
  routeTimeouts.forEach(clearTimeout);
  routeIntervals.forEach(clearInterval);
  routeTimeouts.clear();
  routeIntervals.clear();
}

export function beginRoute() {
  closeRouteResources();
  routeToken += 1;
  routeAbort = new AbortController();
  setRouteAbort(routeAbort);
  return routeToken;
}

export function getRouteToken() {
  return routeToken;
}

export function routeIsCurrent(token) {
  return token === routeToken;
}

export function routeSetTimeout(fn, delay) {
  const timer = setTimeout(() => {
    routeTimeouts.delete(timer);
    fn();
  }, delay);
  routeTimeouts.add(timer);
  return timer;
}

export function routeSetInterval(fn, delay) {
  const timer = setInterval(fn, delay);
  routeIntervals.add(timer);
  return timer;
}

export function setRouteEventSource(source) {
  if (routeEventSource && routeEventSource !== source) routeEventSource.close();
  routeEventSource = source || null;
}

export function setErrorRenderer(renderer) {
  errorRenderer = typeof renderer === 'function' ? renderer : () => {};
}

export function showError(title, detail) {
  errorRenderer(title, detail);
}
