import CoreML
import Vision

/// The owner's output-check settings: block when the detector is at least this sure. 1.0 = never block.
struct FilterSetting: Sendable, Equatable {
    var revealing: Double     // swimwear / lingerie
    var explicit: Double      // nudity
    /// The slider's far right can land a hair under 1.0 (0.3 + 14 × 0.05 in floating point); that is "Off" too.
    /// Before this, "Off" was really "block when 99.99999% sure", so clearly nude results were still blocked.
    static func off(_ v: Double) -> Bool { v >= 0.999 }
    var isOff: Bool { Self.off(revealing) && Self.off(explicit) }
}

/// Graded check of the image generator's OUTPUT (never the user's input photo), using the bundled
/// "Bikini Bottom" Create ML classifier (MIT, github.com/TopScrech/Bikini-Bottom): Bikini / Naked / Regular.
enum Detector {
    struct Scores { var revealing: Double; var explicit: Double }

    private static let model: VNCoreMLModel? = {
        guard let url = Bundle.main.url(forResource: "ContentCheck", withExtension: "mlmodelc"),
              let ml = try? MLModel(contentsOf: url) else { return nil }
        return try? VNCoreMLModel(for: ml)
    }()

    static func scores(_ image: CGImage) -> Scores? {
        guard let model else { return nil }
        let request = VNCoreMLRequest(model: model)
        request.imageCropAndScaleOption = .scaleFit
        try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        guard let results = request.results as? [VNClassificationObservation], !results.isEmpty else { return nil }
        func p(_ label: String) -> Double {
            Double(results.first { $0.identifier.caseInsensitiveCompare(label) == .orderedSame }?.confidence ?? 0)
        }
        return Scores(revealing: p("Bikini"), explicit: p("Naked"))
    }

    static func blocks(_ image: CGImage, _ f: FilterSetting) -> Bool {
        if f.isOff { return false }
        guard let s = scores(image) else { return false }      // checker unavailable: never block everything
        if !FilterSetting.off(f.explicit), s.explicit >= f.explicit { return true }
        if !FilterSetting.off(f.revealing), s.revealing >= f.revealing { return true }
        return false
    }
}
