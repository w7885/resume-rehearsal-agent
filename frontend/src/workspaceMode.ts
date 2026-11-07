const remote = (import.meta as ImportMeta & { env?: Record<string, string> }).env?.VITE_APP_MODE === 'remote';

export const workspaceCopy = {
  storage: remote ? '按账号保存' : '本机保存',
  history: remote ? '消息按账号保存' : '消息保存在本机',
  loading: remote ? '正在读取账号历史…' : '正在读取本机历史…',
  welcome: remote ? '文字和原件会按账号保存，重新登录后可以继续。' : '文字和原件会保存在本机，刷新后可以继续。',
  voiceTitle: remote ? '普通话录音将发送至服务器转写，校对后手动发送' : '普通话录音，本机转写，校对后手动发送',
  transcribing: remote ? '正在服务器转写，已有文字仍保留；切换会话会取消本次结果。' : '正在本机转写，已有文字仍保留；切换会话会取消本次结果。',
};
