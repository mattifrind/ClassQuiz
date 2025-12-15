<!--
SPDX-FileCopyrightText: 2023 Marlon W (Mawoka)

SPDX-License-Identifier: MPL-2.0
-->

<!--suppress ALL -->
<script lang="ts">
	import { socket } from '$lib/socket';
	import JoinGame from '$lib/play/join.svelte';
	import type { Answer, Question as QuestionType } from '$lib/quiz_types';
	import ShowTitle from '$lib/play/title.svelte';
	import Question from '$lib/play/question.svelte';
	import { navbarVisible } from '$lib/stores.svelte.ts';
	import ShowEndScreen from '$lib/play/admin/final_results.svelte';
	import KahootResults from '$lib/play/results_kahoot.svelte';
	import { getLocalization } from '$lib/i18n';
	import Cookies from 'js-cookie';
	const { t } = getLocalization();

	const PLAYER_COOKIE = 'player_identity';

	interface Props {
		// Exports
		data: any;
	}

	let { data }: Props = $props();
	let { game_pin } = $state(data);

	// Types
	interface GameMeta {
		started: boolean;
	}

	let game_mode = $state();
	let final_results: Array<null> | Array<Array<PlayerAnswer>> = $state([null]);

	interface PlayerAnswer {
		username: string;
		answer: string;
		right: string;
	}

	// Variables init
	let question_index = $state('');
	let unique = $state({});
	navbarVisible.visible = false;
	let answer_results: Array<Answer> = $state();
	let gameData = $state();
	let solution: QuestionType = $state();
	let username = $state('');
	let scores = $state({});
	let gameMeta: GameMeta = $state({
		started: false
	});

	let question: Question = $state();

	let preventReload = true;

	function leaveGame() {
		preventReload = false;
		Cookies.remove(PLAYER_COOKIE);
		try {
			socket.emit('leave_game');
		} catch {
			// ignore
		}
		try {
			socket.close();
		} catch {
			// ignore
		}
		gameData = undefined;
		gameMeta.started = false;
		question_index = '';
		answer_results = undefined;
		solution = undefined;
		question = undefined;
		username = '';
		game_pin = '';
	}

	// Functions
	function restart() {
		unique = {};
	}

	const confirmUnload = (event: Event) => {
		if (preventReload) {
			event.preventDefault();
			// eslint-disable-next-line @typescript-eslint/ban-ts-comment
			// @ts-ignore
			event.returnValue = '';
		}
	};

	// Ensure socket listeners are registered exactly once even if the page component
	// is created multiple times during navigation/hot reload.
	let __socketHandlersRegistered = false;
	const registerSocketHandlers = () => {
		if (__socketHandlersRegistered) return;
		__socketHandlersRegistered = true;

		socket.on('time_sync', (data) => {
			socket.emit('echo_time_sync', data);
		});

		socket.on('connect', async () => {
			console.log('Connected!');
			// Default to normal until server tells us otherwise.
			if (game_mode === undefined) {
				game_mode = 'normal';
			}
			const cookie_data = Cookies.get(PLAYER_COOKIE);
			if (cookie_data) {
				try {
					const parsed = JSON.parse(cookie_data);
					if (parsed?.player_token && parsed?.game_pin) {
						socket.emit('rejoin_game', {
							player_token: parsed.player_token,
							game_pin: parsed.game_pin
						});
						// Ask explicitly for current state after reconnect/rejoin.
						setTimeout(() => {
							try {
								socket.emit('get_current_question');
							} catch {
								// ignore
							}
						}, 250);
					} else {
						Cookies.remove(PLAYER_COOKIE);
					}
				} catch {
					Cookies.remove(PLAYER_COOKIE);
				}
			}

			// game_mode may be needed for rendering; best-effort refresh
			try {
				const res = await fetch(`/api/v1/quiz/play/check_captcha/${game_pin}`);
				const json = await res.json();
				game_mode = json.game_mode;
			} catch {
				// ignore
			}
		});

		// Socket-events
		socket.on('joined_game', (data) => {
			gameData = data;
			if (typeof (globalThis as any).plausible === 'function') {
				(globalThis as any).plausible('Joined Game', {
					props: { game_id: (gameData as any).game_id }
				});
			}
			// Cookie set via `player_identity` event
		});
		socket.on('rejoined_game', (data) => {
			gameData = data;
			if (data.started) {
				gameMeta.started = true;
			}
			// Force Svelte to consider question screen on active games.
			question_index = question_index;
		});

		socket.on('game_not_found', () => {
			const cookie_data = Cookies.get(PLAYER_COOKIE);
			if (cookie_data) {
				Cookies.remove(PLAYER_COOKIE);
				window.location.reload();
				return;
			}
		});

		socket.on('player_identity', (data) => {
			Cookies.set(PLAYER_COOKIE, JSON.stringify(data), {
				expires: 30,
				sameSite: 'Lax',
				path: '/'
			});
		});

		socket.on('captcha_failed', () => {
			window.alert('Captcha failed. Please try again.');
		});

		socket.on('rejoin_failed', () => {
			Cookies.remove(PLAYER_COOKIE);
			window.location.reload();
		});

		socket.on('connect_error', (err) => {
			console.error('Socket connect_error', err);
		});

		socket.on('set_question_number', (data) => {
			solution = undefined;
			restart();
			gameMeta.started = true;
			question = { ...data.question, started_at: data.started_at };
			question_index = data.question_index;
			answer_results = undefined;
		});

		socket.on('start_game', () => {
			gameMeta.started = true;
		});

		socket.on('question_results', (data) => {
			restart();
			answer_results = data;
		});

		socket.on('username_already_exists', () => {
			window.alert('Username already exists!');
		});

		socket.on('kick', () => {
			window.alert('You got kicked');
			preventReload = false;
			game_pin = '';
			username = '';
			Cookies.set('kicked', 'value', { expires: 1 });
			window.location.reload();
		});
		socket.on('final_results', (data) => {
			final_results = data;
			Cookies.remove(PLAYER_COOKIE);
		});

		socket.on('solutions', (data) => {
			solution = data;
		});
	};

	let bg_color = $derived(gameData ? (gameData as any).background_color : undefined);

	registerSocketHandlers();

	// The rest
</script>

<svelte:window onbeforeunload={confirmUnload} />
<svelte:head>
	<title>ClassQuiz - Play</title>
</svelte:head>
<div
	class="min-h-screen min-w-full"
	style="background: {bg_color ? bg_color : 'transparent'}"
	class:text-black={bg_color}
>
	{#if gameData !== undefined}
		<button
			type="button"
			onclick={leaveGame}
			class="fixed top-4 right-4 z-50 rounded-lg bg-white/80 px-3 py-2 text-sm font-semibold text-black shadow-lg backdrop-blur hover:bg-white"
		>
			Leave
		</button>
	{/if}
	<div>
		{#if !gameMeta.started && gameData === undefined}
			<JoinGame bind:game_pin bind:game_mode bind:username />
		{:else if JSON.stringify(final_results) !== JSON.stringify([null])}
			<ShowEndScreen bind:data={scores} show_final_results={true} {username} />
		{:else if gameData !== undefined && question_index === ''}
			<ShowTitle
				title={gameData.title}
				description={gameData.description}
				cover_image={gameData.cover_image}
			/>
		{:else if gameMeta.started && gameData !== undefined && question_index !== '' && answer_results === undefined}
			{#key unique}
				<div class="text-black dark:text-black">
					<Question bind:game_mode bind:question {question_index} {solution} />
				</div>
			{/key}
		{:else if gameMeta.started && answer_results !== undefined}
			{#if answer_results === null}
				<div class="w-full flex justify-center">
					<h1 class="text-3xl">{$t('admin_page.no_answers')}</h1>
				</div>
			{:else}
				<div>
					<h2 class="text-center text-3xl mb-8">{$t('words.result', { count: 2 })}</h2>
				</div>
				{#key unique}
					<KahootResults {username} question_results={answer_results} bind:scores />
				{/key}
			{/if}
		{/if}
	</div>
</div>
