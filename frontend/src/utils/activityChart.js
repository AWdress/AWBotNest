// Both series use the same bounded easing so smoothing cannot invent a peak
// or make the success count cross above the trigger count between samples.
export function activityCurvePath(points, valueKey = 'y') {
  if (!points.length) return ''
  let path = `M ${points[0].x} ${points[0][valueKey]}`
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1]
    const current = points[index]
    const controlWidth = (current.x - previous.x) / 3
    path += ` C ${previous.x + controlWidth} ${previous[valueKey]}`
      + ` ${current.x - controlWidth} ${current[valueKey]}`
      + ` ${current.x} ${current[valueKey]}`
  }
  return path
}
