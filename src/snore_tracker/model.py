"""YAMNet TFLite: one 0.975 s window in, 521 AudioSet class scores out."""

from ai_edge_litert.interpreter import Interpreter

SNORING = 38  # index in yamnet_class_map.csv
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite"


class Yamnet:
    def __init__(self, path):
        self.interp = Interpreter(model_path=str(path))
        self.interp.allocate_tensors()
        self.input = self.interp.get_input_details()[0]["index"]
        self.output = self.interp.get_output_details()[0]["index"]

    def snore_score(self, window):
        self.interp.set_tensor(self.input, window)
        self.interp.invoke()
        return float(self.interp.get_tensor(self.output)[0][SNORING])
