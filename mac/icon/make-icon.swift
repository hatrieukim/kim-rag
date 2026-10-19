// Draws the kim-rag app icon: a sound wave that turns into dots on a timeline, the moment that is "now"
// in amber. Run with `swift make-icon.swift OUT.png [size]`; build.sh turns it into AppIcon.icns.
import AppKit
import CoreGraphics

let args = CommandLine.arguments
let out = URL(fileURLWithPath: args.count > 1 ? args[1] : "icon.png")
let size = args.count > 2 ? Int(args[2]) ?? 1024 : 1024
let s = CGFloat(size) / 1024   // everything below is drawn on a 1024 grid

let space = CGColorSpace(name: CGColorSpace.sRGB)!
let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.scaleBy(x: s, y: s)

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

// The body: Apple's macOS grid, 824 pt square with rounded corners, a soft shadow under it.
let body = CGRect(x: 100, y: 100, width: 824, height: 824)
let shape = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 24, color: color(0x000000, 0.35))
ctx.addPath(shape)
ctx.setFillColor(color(0x1B1F4B))
ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(shape)
ctx.clip()
let background = CGGradient(colorsSpace: space, colors: [color(0x3B2F8F), color(0x1A5E9A), color(0x0E9AA0)] as CFArray,
                            locations: [0, 0.55, 1])!
ctx.drawLinearGradient(background, start: CGPoint(x: 140, y: 924), end: CGPoint(x: 884, y: 100), options: [])
// A faint light from the top so the face does not look flat.
let glow = CGGradient(colorsSpace: space, colors: [color(0xFFFFFF, 0.18), color(0xFFFFFF, 0)] as CFArray,
                      locations: [0, 1])!
ctx.drawRadialGradient(glow, startCenter: CGPoint(x: 400, y: 900), startRadius: 0,
                       endCenter: CGPoint(x: 400, y: 900), endRadius: 620, options: [])
ctx.restoreGState()

let mid: CGFloat = 512
// The wave: bars that grow and fade out to the right, where they become the timeline.
let heights: [CGFloat] = [150, 300, 470, 580, 420, 260, 140]
var x: CGFloat = 190
let bar: CGFloat = 34
let gap: CGFloat = 18
for (i, h) in heights.enumerated() {
    let alpha = i < 4 ? 1 : 1 - CGFloat(i - 3) * 0.15
    let rect = CGRect(x: x, y: mid - h / 2, width: bar, height: h)
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: bar / 2, cornerHeight: bar / 2, transform: nil))
    ctx.setFillColor(color(0xFFFFFF, alpha))
    ctx.fillPath()
    x += bar + gap
}

// The timeline: a thin line with the moments kept, the last one is now.
let lineStart = x + 20
ctx.setStrokeColor(color(0xFFFFFF, 0.45))
ctx.setLineWidth(10)
ctx.setLineCap(.round)
ctx.move(to: CGPoint(x: lineStart, y: mid))
ctx.addLine(to: CGPoint(x: 795, y: mid))
ctx.strokePath()
let dots: [(CGFloat, CGFloat)] = [(625, 16), (702, 20)]
for (cx, r) in dots {
    ctx.setFillColor(color(0xFFFFFF))
    ctx.fillEllipse(in: CGRect(x: cx - r, y: mid - r, width: r * 2, height: r * 2))
}
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 30, color: color(0xFFC857, 0.9))
ctx.setFillColor(color(0xFFC857))
ctx.fillEllipse(in: CGRect(x: 795 - 36, y: mid - 36, width: 72, height: 72))
ctx.restoreGState()

let image = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: image)
try rep.representation(using: .png, properties: [:])!.write(to: out)
