/**
 * 启动屏专用 preload：只暴露一个「跳过」动作。
 *
 * 不用 before-input-event 的原因：CDP 注入和真实 OS 鼠标点击都没能稳定触发它
 * （实测收幕仍要等满 2.6s）。DOM 的 pointerdown 是页面自己的事件，一定能到；
 * 预加载脚本只桥接这一件事，不给启动屏开 nodeIntegration，也不放宽 sandbox。
 */
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("blankSplash", {
  skip: () => ipcRenderer.send("splash:skip"),
});
