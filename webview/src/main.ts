// SPA entry point — mounts the root Svelte component into #app.
//
// The hash router lives inside App.svelte; this file does nothing but
// import the global stylesheet and wire the component to the DOM.

import { mount } from 'svelte';
import App from './App.svelte';
import './styles/app.css';

const target = document.getElementById('app');
if (!target) throw new Error('expected #app container in index.html');

const app = mount(App, { target });
export default app;
