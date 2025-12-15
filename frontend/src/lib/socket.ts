// SPDX-FileCopyrightText: 2023 Marlon W (Mawoka)
//
// SPDX-License-Identifier: MPL-2.0

import { io } from 'socket.io-client';

// IMPORTANT:
// - In the browser, connect to the current origin (Caddy will proxy /socket.io).
// - During SvelteKit SSR, do NOT create a real socket client; it can end up with
//   ws://undefined/... and interfere with runtime state.

const isBrowser = typeof window !== 'undefined';
const baseUrl = isBrowser && window.location?.origin ? window.location.origin : undefined;

export const socket = io(baseUrl, {
	autoConnect: isBrowser,
	path: '/socket.io',
	transports: ['websocket', 'polling']
});

export function ensureSocketConnected() {
	if (!isBrowser) return;
	if (!socket.connected) socket.connect();
}
