export const offsetText = (offset: number) => `本机比参考时间${offset >= 0 ? '快' : '慢'} ${Math.abs(offset).toFixed(2)} ms`
