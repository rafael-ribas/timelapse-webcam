"""app_updated10.py (V10)

Gerador de Timelapse com PyQt5 + OpenCV + ffmpeg.

Principais recursos:
- Seleção de webcam (QComboBox)
- Live Preview opcional (QCheckBox) para economizar CPU quando rodando em segundo plano
- Captura de frames em intervalos configuráveis
- Progresso (frames gerados / previstos) e countdown no rodapé (QStatusBar)
- Geração automática do vídeo (ffmpeg via QProcess) e abertura da pasta ao finalizar
- Bloqueio de campos Intervalo/Tempo Total durante a captura para evitar inconsistências

Observação: este script espera que o arquivo "camera.ui" esteja na mesma pasta.
"""

import os
import sys
import time
import math
import platform
import subprocess

# ---------------------------------------------------------
# OpenCV costuma logar mensagens "barulhentas" em alguns PCs,
# especialmente relacionadas a backends/câmeras UVC (obsensor).
# Para app final (GUI), isso só polui o console.
# ---------------------------------------------------------
DEBUG = False
if not DEBUG:
    os.environ["OPENCV_LOG_LEVEL"] = "OFF"

import cv2

from PyQt5.QtWidgets import QApplication, QMainWindow, QFileDialog
from PyQt5 import uic
from PyQt5.QtCore import QTimer, QProcess
from PyQt5.QtGui import QImage, QPixmap


# ---------------------------------------------------------
# Utilitários (funções pequenas e independentes)
# ---------------------------------------------------------
def formatar_duracao(segundos: float) -> str:
    """Converte segundos em 'mm:ss' ou 'hh:mm:ss' (quando hh>0)."""
    s = max(0, int(round(segundos)))
    hh, resto = divmod(s, 3600)
    mm, ss = divmod(resto, 60)
    if hh > 0:
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def abrir_pasta_no_sistema(pasta: str) -> None:
    """Abre a pasta no explorador de arquivos do sistema operacional."""
    if not pasta or not os.path.isdir(pasta):
        return
    try:
        if platform.system() == "Windows":
            os.startfile(pasta)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", pasta])
        else:
            subprocess.Popen(["xdg-open", pasta])
    except Exception:
        # Não é crítico: apenas não abre a pasta.
        pass


class TimeLapseApp(QMainWindow):
    """Janela principal.

    A lógica do app foi separada em blocos:
    - Preview (timer rápido ~30ms): lê frames continuamente e atualiza labelCamera
    - Timelapse (timer lento = intervalo): salva 1 frame por intervalo
    - UI (timer leve 250ms): atualiza rodapé com progresso/tempo restante

    Importante: quando o Live Preview está DESLIGADO, o app evita atualizar a UI
    com frames e só lê a câmera no exato momento de salvar o frame do timelapse.
    """

    def __init__(self):
        super().__init__()

        # Carrega a interface criada no Qt Designer.
        # (camera.ui deve estar na mesma pasta do .py)
        uic.loadUi("camera.ui", self)

        # Define o ícone da janela (caminho absoluto)
        from PyQt5.QtGui import QIcon
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        self.setWindowIcon(QIcon(os.path.join(BASE_DIR, "icone.png")))

        # =============================
        # ESTADO / VARIÁVEIS DE CONTROLE
        # =============================
        self.cap = None  # cv2.VideoCapture ativo
        self.camera_index_atual = 0  # índice da webcam selecionada
        self.frame_atual = None  # último frame capturado (numpy array)

        # Controle do timelapse
        self.inicio = None
        self.fim_previsto = None
        self.contador = 0
        self.frames_previstos = 0
        self.pasta_execucao = None  # pasta do "lote" atual
        self.video_path_ultimo = None  # mp4 gerado por último

        # =============================
        # TIMERS
        # =============================

        # Timer do preview: roda rápido, deixa a imagem fluida no QLabel.
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self.atualizar_preview)

        # Timer do timelapse: roda no intervalo escolhido e salva frames.
        self.capture_timer = QTimer(self)
        self.capture_timer.timeout.connect(self.capturar_frame)

        # Timer "leve" de UI: atualiza rodapé (progresso/contagem regressiva).
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.atualizar_rodape)
        self.ui_timer.start(250)

        # =============================
        # PROCESSO FFMPEG (assíncrono)
        # =============================

        # Usamos QProcess (Qt) em vez de subprocess.Popen para saber quando termina.
        self.ffmpeg_process = QProcess(self)
        self.ffmpeg_process.finished.connect(self.video_finalizado)

        # =============================
        # CONEXÕES DE UI
        # =============================

        # Botão principal: inicia/para o timelapse.
        self.btnIniciarCamera.setText("Iniciar timelapse")
        self.btnIniciarCamera.clicked.connect(self.toggle_timelapse)

        # Combo de câmera (se existir na UI)
        if hasattr(self, "comboCamera"):
            self.comboCamera.currentIndexChanged.connect(self.trocar_camera_por_combo)

        # Atualiza previsões quando usuário altera parâmetros
        if hasattr(self, "spinIntervalo"):
            self.spinIntervalo.valueChanged.connect(self.atualizar_previsoes)
        if hasattr(self, "spinTempoTotal"):
            self.spinTempoTotal.valueChanged.connect(self.atualizar_previsoes)
        if hasattr(self, "spinFPS"):
            self.spinFPS.valueChanged.connect(self.atualizar_previsoes)

        # Checkbox de preview: seu objectName no .ui (pela sua captura) é "checkBox"
        self.cb_preview = self._encontrar_checkbox_preview()
        if self.cb_preview is not None:
            try:
                self.cb_preview.toggled.connect(self._on_preview_toggled)
            except Exception:
                pass

        # =============================
        # INICIALIZAÇÃO
        # =============================
        self.detectar_cameras()                      # preenche comboCamera
        self.abrir_camera(self.camera_index_atual)   # abre câmera inicial
        self._aplicar_estado_preview_inicial()       # liga/desliga preview conforme checkbox
        self.atualizar_previsoes()                   # escreve previsão inicial no rodapé/campo

    # =====================================================
    # BLOQUEIO DE CONTROLES DURANTE CAPTURA
    # =====================================================
    def _bloquear_controles_captura(self, bloqueado: bool):
        """Trava/destrava widgets que não devem mudar durante a captura."""
        # Intervalo e Tempo Total: travar durante o timelapse
        if hasattr(self, "spinIntervalo"):
            self.spinIntervalo.setEnabled(not bloqueado)
        if hasattr(self, "spinTempoTotal"):
            self.spinTempoTotal.setEnabled(not bloqueado)

        # Evitar trocar a câmera no meio da captura
        if hasattr(self, "comboCamera"):
            self.comboCamera.setEnabled(not bloqueado)

        # FPS: pode ficar editável porque só afeta a renderização do vídeo.
        # Se preferir travar também, descomente:
        # if hasattr(self, "spinFPS"):
        #     self.spinFPS.setEnabled(not bloqueado)

    # =====================================================
    # PREVIEW: checkbox
    # =====================================================
    def _encontrar_checkbox_preview(self):
        """Localiza a checkbox do preview."""
        nomes = [
            "checkBox"
        ]
        for n in nomes:
            if hasattr(self, n):
                return getattr(self, n)

        # fallback: primeira QCheckBox encontrada
        try:
            from PyQt5.QtWidgets import QCheckBox
            cbs = self.findChildren(QCheckBox)
            if cbs:
                return cbs[0]
        except Exception:
            pass
        return None

    def preview_habilitado(self) -> bool:
        """Retorna True se o Live Preview estiver ligado."""
        if self.cb_preview is None:
            return True
        try:
            return bool(self.cb_preview.isChecked())
        except Exception:
            return True

    def _aplicar_estado_preview_inicial(self):
        """Liga/desliga o preview ao iniciar o programa."""
        if self.preview_habilitado():
            self.preview_timer.start(30)
        else:
            self.preview_timer.stop()
            try:
                self.labelCamera.setText("Live Preview Desligado")
            except Exception:
                pass

    def _on_preview_toggled(self, checked: bool):
        """Callback do toggle da checkbox do preview."""
        if checked:
            self.preview_timer.start(30)
        else:
            self.preview_timer.stop()
            try:
                self.labelCamera.setText("Live Preview Desligado")
            except Exception:
                pass

    # =====================================================
    # CÂMERAS
    # =====================================================
    def detectar_cameras(self, max_testes: int = 6):
        """Detecta índices de câmera disponíveis."""
        self.cameras = []
        falhas = 0

        for i in range(max_testes):
            cap = cv2.VideoCapture(i)
            if not cap.isOpened():
                cap.release()
                falhas += 1
                if falhas >= 2:
                    break
                continue

            ret, _ = cap.read()
            cap.release()

            if ret:
                self.cameras.append(i)
                falhas = 0
            else:
                falhas += 1
                if falhas >= 2:
                    break

        if hasattr(self, "comboCamera"):
            self.comboCamera.clear()
            for idx in self.cameras:
                self.comboCamera.addItem(f"Câmera {idx}", idx)
            if self.cameras:
                self.comboCamera.setCurrentIndex(0)

    def abrir_camera(self, index: int) -> bool:
        """Abre a câmera por índice (backend padrão do OpenCV)."""
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            return False

        ret, frame = cap.read()
        if not ret:
            cap.release()
            return False

        if self.cap:
            self.cap.release()

        self.cap = cap
        self.camera_index_atual = index
        self.frame_atual = frame
        return True

    def trocar_camera_por_combo(self):
        """Troca câmera quando usuário muda no combo (somente fora da captura)."""
        if not hasattr(self, "comboCamera"):
            return

        novo = self.comboCamera.currentData()
        if novo is None:
            return

        # Não trocar câmera durante timelapse
        if self.capture_timer.isActive():
            return

        preview_estava = self.preview_timer.isActive()
        self.preview_timer.stop()

        ok = self.abrir_camera(int(novo))
        if not ok:
            fallback = self.cameras[0] if getattr(self, "cameras", None) else 0
            self.abrir_camera(fallback)
            i = self.comboCamera.findData(fallback)
            if i >= 0:
                self.comboCamera.blockSignals(True)
                self.comboCamera.setCurrentIndex(i)
                self.comboCamera.blockSignals(False)

        if preview_estava and self.preview_habilitado() and self.cap:
            self.preview_timer.start(30)

    # =====================================================
    # PREVIEW
    # =====================================================
    def atualizar_preview(self):
        """Atualiza o QLabel com imagem da câmera (quando preview habilitado)."""
        if not self.preview_habilitado():
            return
        if not self.cap:
            return

        ret, frame = self.cap.read()
        if not ret:
            return

        self.frame_atual = frame

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self.labelCamera.setPixmap(QPixmap.fromImage(img))

    # =====================================================
    # PREVISÕES
    # =====================================================
    def atualizar_previsoes(self):
        """Atualiza frames previstos e duração prevista do vídeo."""
        try:
            intervalo = max(1, int(self.spinIntervalo.value()))
            tempo_total = max(0, int(self.spinTempoTotal.value()))
            fps_video = max(1, int(self.spinFPS.value()))
        except Exception:
            return

        frames_previstos = math.ceil(tempo_total / intervalo) if intervalo > 0 else 0
        dur_video = int(round(frames_previstos / fps_video)) if frames_previstos else 0

        try:
            self.lineEdit_2.setText(str(frames_previstos))
        except Exception:
            pass

        if not self.capture_timer.isActive():
            try:
                self.statusbar.showMessage(
                    f"Previsto: {frames_previstos} frames | Vídeo: {formatar_duracao(dur_video)}"
                )
            except Exception:
                pass

    # =====================================================
    # TIMELAPSE
    # =====================================================
    def toggle_timelapse(self):
        """Inicia ou para o timelapse (botão único)."""
        if self.capture_timer.isActive():
            self.parar_timelapse()
        else:
            self.iniciar_timelapse()

    def iniciar_timelapse(self):
        """Configura e inicia a captura de frames."""
        base = QFileDialog.getExistingDirectory(self, "Escolha a pasta de saída")
        if not base:
            return

        ts = time.strftime("timelapse_%Y%m%d_%H%M%S")
        self.pasta_execucao = os.path.join(base, ts)
        os.makedirs(self.pasta_execucao, exist_ok=True)

        intervalo = max(1, int(self.spinIntervalo.value()))
        tempo_total = max(0, int(self.spinTempoTotal.value()))

        self.frames_previstos = math.ceil(tempo_total / intervalo)
        self.contador = 0

        self.progressBar.setMaximum(max(1, self.frames_previstos))
        self.progressBar.setValue(0)

        self.lineEdit_2.setText(str(self.frames_previstos))
        self.lineEdit.setText("0")

        self.inicio = time.time()
        self.fim_previsto = self.inicio + tempo_total

        fps_video = max(1, int(self.spinFPS.value()))
        dur_video = int(round(self.frames_previstos / fps_video)) if self.frames_previstos else 0

        self.capture_timer.start(intervalo * 1000)
        self.btnIniciarCamera.setText("Parar timelapse")
        self._bloquear_controles_captura(True)

        self.statusbar.showMessage(
            f"Timelapse iniciado: {self.frames_previstos} frames | Vídeo: {formatar_duracao(dur_video)}"
        )

    def capturar_frame(self):
        """Salva um frame do timelapse."""
        if not self.cap:
            return

        # Se preview estiver desligado, lê a câmera só agora (economiza CPU)
        if self.preview_habilitado():
            frame = self.frame_atual
        else:
            ret, f = self.cap.read()
            frame = f if ret else None

        if frame is None or (hasattr(frame, "size") and frame.size == 0):
            return

        if time.time() >= self.fim_previsto:
            self.parar_timelapse()
            return

        nome = f"frame_{self.contador:05d}.jpg"
        caminho = os.path.join(self.pasta_execucao, nome)
        cv2.imwrite(caminho, frame)

        self.contador += 1
        self.lineEdit.setText(str(self.contador))
        self.progressBar.setValue(min(self.contador, self.progressBar.maximum()))

    def parar_timelapse(self):
        """Para a captura e inicia a renderização do vídeo."""
        self.capture_timer.stop()
        self.btnIniciarCamera.setText("Iniciar timelapse")
        self._bloquear_controles_captura(False)
        self.gerar_video()

    # =====================================================
    # STATUS / VÍDEO
    # =====================================================
    def atualizar_rodape(self):
        """Enquanto capturando, mostra progresso + countdown no rodapé."""
        if not self.capture_timer.isActive() or not self.fim_previsto:
            return

        restante = max(0, int(self.fim_previsto - time.time()))
        mm, ss = divmod(restante, 60)

        pct = int((self.contador / self.frames_previstos) * 100) if self.frames_previstos else 0

        try:
            fps_video = max(1, int(self.spinFPS.value()))
        except Exception:
            fps_video = 30
        dur_video = int(round(self.frames_previstos / fps_video)) if self.frames_previstos else 0

        self.statusbar.showMessage(
            f"{self.contador}/{self.frames_previstos} frames ({pct}%)"
            f" | Faltam {mm:02d}:{ss:02d} | Vídeo: {formatar_duracao(dur_video)}"
        )

    def gerar_video(self):
        """Dispara o ffmpeg em background para gerar o MP4."""
        if not self.pasta_execucao or self.contador == 0:
            self.statusbar.showMessage("Nenhum frame para gerar vídeo.")
            return

        fps = max(1, int(self.spinFPS.value()))
        output = os.path.join(self.pasta_execucao, "timelapse.mp4")
        self.video_path_ultimo = output

        args = [
            "-y",
            "-framerate", str(fps),
            "-i", "frame_%05d.jpg",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            output,
        ]

        self.ffmpeg_process.setWorkingDirectory(self.pasta_execucao)
        self.ffmpeg_process.start("ffmpeg", args)
        self.statusbar.showMessage("Gerando vídeo...")

    def video_finalizado(self, exitCode, exitStatus):
        """Callback do ffmpeg: atualiza status e abre pasta."""
        if exitCode == 0:
            msg = "Vídeo gerado com sucesso ✅"
            if self.video_path_ultimo:
                msg = f"Vídeo gerado: {self.video_path_ultimo} ✅"
            self.statusbar.showMessage(msg)

            self.abrir_pasta_saida()
        else:
            self.statusbar.showMessage("Erro ao gerar vídeo ❌")

    def abrir_pasta_saida(self):
        """Abre a pasta da execução atual (frames + mp4)."""
        if self.pasta_execucao:
            abrir_pasta_no_sistema(self.pasta_execucao)

    # =====================================================
    # FECHAR
    # =====================================================
    def closeEvent(self, event):
        """Libera timers e câmera ao fechar o app."""
        self.capture_timer.stop()
        self.preview_timer.stop()
        if self.cap:
            self.cap.release()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = TimeLapseApp()
    w.show()
    sys.exit(app.exec_())
