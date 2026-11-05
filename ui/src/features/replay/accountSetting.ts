/** 旧账户缺少字段时视为关闭；只提交用户实际改变的录制意愿。 */
export function replayRecordingPatch(enabled: boolean, saved?: boolean): { replay_recording_enabled?: boolean } {
  return enabled === (saved ?? false) ? {} : { replay_recording_enabled: enabled }
}
