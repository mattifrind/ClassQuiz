// SPDX-FileCopyrightText: 2023 Marlon W (Mawoka)
//
// SPDX-License-Identifier: MPL-2.0

import { io } from 'socket.io-client';

// Ensure we always have a valid base URL.
// Default to current origin (works behind Caddy during dev/prod).
const baseUrl =
	(typeof window !== 'undefined' && window.location?.origin ? window.location.origin : undefined) ??
	undefined;

export const socket = io(baseUrl, {
	autoConnect: true,
	transports: ['websocket', 'polling']
});

export function ensureSocketConnected() {
	if (!socket.connected) {
		socket.connect();
	}
}
