"""Current vector control methods for synchronous machine drives.

"""

from dataclasses import dataclass, InitVar
import numpy as np
from motulator._helpers import abc2complex
from motulator.control._common import Ctrl, ComplexPICtrl, PWM, SpeedCtrl
from motulator.control.sm._torque import TorqueCharacteristics
from motulator._utils import Bunch


# %%
@dataclass
class ModelPars:
    """
    Model parameters of a synchronous machine.
    
    Parameters
    ----------
    R_s : float
        Stator resistance (Ω).
    L_d : float
        d-axis inductance (H).
    L_q : float
        q-axis inductance (H).
    psi_f : float
        PM flux linkage (Vs).
    n_p : int
        Number of pole pairs.
    J : float
        Moment of inertia (kgm²).

    """
    R_s: float = None
    L_d: float = None
    L_q: float = None
    psi_f: float = None
    n_p: int = None
    J: float = None


# %%
class VectorCtrl(Ctrl):
    """
    Vector control for synchronous machine drives.

    This class interconnects the subsystems of the control system and
    provides the interface to the solver.

    Parameters
    ----------
    par : ModelPars
        Machine model parameters.
    ref : ReferencePars
        Reference generation parameters.
    T_s : float, optional
        Sampling period (s). The default is 250e-6.
    sensorless : bool, optional
        If True, sensorless control is used. The default is True.

    Attributes
    ----------
    current_ref : CurrentReference
        Current reference generator.
    observer : Observer
        Flux and rotor position observer, used in the sensorless mode only.
    current_ctrl : CurrentCtrl
        Current controller.
    speed_ctrl : SpeedCtrl 
        Speed controller.
    pwm : PWM
        Pulse-width modulation.
    w_m_ref : callable
        Speed reference (electrical rad/s) as a function of time (s).
        
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, par, ref, T_s=250e-6, sensorless=True):
        super().__init__()
        self.T_s = T_s
        self.sensorless = sensorless
        self.n_p = par.n_p
        self.current_ref = CurrentReference(par, ref)
        self.current_ctrl = CurrentCtrl(par, 2*np.pi*200)
        if sensorless:
            self.observer = Observer(par, alpha_o=2*np.pi*100, sensorless=True)
        else:
            self.observer = None
        self.speed_ctrl = SpeedCtrl(par.J, 2*np.pi*4)
        self.pwm = PWM()
        self.w_m_ref = callable

    # pylint: disable=too-many-locals
    def __call__(self, mdl):
        """
        Run the main control loop.

        Parameters
        ----------
        mdl : Drive
            Continuous-time system model for getting the feedback signals.

        Returns
        -------
        T_s : float
            Sampling period (s).
        d_abc : ndarray, shape (3,)
            Duty ratios.

        """
        # Get the speed reference
        w_m_ref = self.w_m_ref(self.clock.t)

        # Measure the feedback signals
        i_s_abc = mdl.machine.meas_currents()
        u_dc = mdl.converter.meas_dc_voltage()
        u_s = self.pwm.realized_voltage

        # Get the states
        if self.sensorless:
            w_m, theta_m = self.observer.w_m, self.observer.theta_m
        else:
            w_m = self.n_p*mdl.mechanics.meas_speed()
            theta_m = self.n_p*mdl.mechanics.meas_position()
            theta_m = np.mod(theta_m + np.pi, 2*np.pi) - np.pi

        # Current vector in estimated rotor coordinates
        i_s = np.exp(-1j*theta_m)*abc2complex(i_s_abc)

        # Outputs
        tau_M_ref = self.speed_ctrl.output(w_m_ref/self.n_p, w_m/self.n_p)
        i_s_ref, tau_M_ref_lim = self.current_ref.output(tau_M_ref, w_m, u_dc)
        u_s_ref = self.current_ctrl.output(i_s_ref, i_s)

        # Data logging
        data = Bunch(
            i_s=i_s,
            i_s_ref=i_s_ref,
            t=self.clock.t,
            tau_M_ref_lim=tau_M_ref_lim,
            theta_m=theta_m,
            u_dc=u_dc,
            u_s=u_s,
            w_m=w_m,
            w_m_ref=w_m_ref,
        )
        self.save(data)

        # Update states
        if self.sensorless:
            self.observer.update(self.T_s, u_s, i_s)
        self.speed_ctrl.update(self.T_s, tau_M_ref_lim)
        self.current_ref.update(self.T_s, tau_M_ref_lim, u_s_ref, u_dc)
        self.current_ctrl.update(self.T_s, u_s, w_m)
        self.clock.update(self.T_s)

        d_abc = self.pwm(self.T_s, u_s_ref, u_dc, theta_m, w_m)

        return self.T_s, d_abc


# %%
class CurrentCtrl(ComplexPICtrl):
    """
    Current controller for synchronous machines.

    This provides an interface of a current controller for synchronous machines
    [#Awa2019]_. The gains are initialized based on the desired closed-loop 
    bandwidth and the inductances. 

    Parameters
    ----------
    par : ModelPars
        Synchronous machine parameters, should contain `L_d` and `L_q` (H). 
    alpha_c : float
        Closed-loop bandwidth (rad/s).

    References
    ----------
    .. [#Awa2019] Awan, Saarakkala, Hinkkanen, "Flux-linkage-based current 
       control of saturated synchronous motors," IEEE Trans. Ind. Appl. 2019,
       https://doi.org/10.1109/TIA.2019.2919258

    """

    def __init__(self, par, alpha_c):
        k_p, k_i, k_t = 2*alpha_c, alpha_c**2, alpha_c
        super().__init__(k_p, k_i, k_t)
        self.L_d = par.L_d
        self.L_q = par.L_q

    def output(self, i_ref, i):
        # Extends the base class method by transforming the currents to the
        # flux linkages, which is a simple way to take the saliency into account
        psi_ref = self.L_d*i_ref.real + 1j*self.L_q*i_ref.imag
        psi = self.L_d*i.real + 1j*self.L_q*i.imag
        return super().output(psi_ref, psi)


# %%
# pylint: disable=too-many-instance-attributes
@dataclass
class CurrentReferencePars:
    """
    Parameters for reference generation.

    Parameters
    ----------
    par : ModelPars
        Machine model parameters.
    i_s_max : float
        Maximum stator current (A). 
    psi_s_min : float, optional
        Minimum stator flux (Vs). The default is `psi_f`.
    w_m_nom : float, optional
        Nominal rotor angular speed (electrical rad/s). Needed if `k_fw` is not
        directly provided.
    alpha_fw : float, optional
        Field-weakening bandwidth (rad/s). The default is 2*pi*20.
    k_fw : float, optional
        Field-weakening gain. The default is `alpha_fw/(w_m_nom*par.L_d)`.
    k_u : float, optional
        Voltage utilization factor. The default is 0.95.

    Attributes
    ----------
    i_sd_mtpa : callable
        MTPA d-axis current (A) as a funtion of the torque (Nm).
    tau_M_lim : callable
        Torque limite (Nm) as a function of the stator flux linkage (Vs). This
        limit merges the MTPV and current limits.
    i_sd_lim : callable
        d-axis current limit (A) as a function of the stator flux linkage (Vs).
        This limit merges the MTPV and current limits.
    
    """
    par: InitVar[ModelPars]
    i_s_max: float
    psi_s_min: float = None
    w_m_nom: InitVar[float] = None
    alpha_fw: InitVar[float] = 2*np.pi*20
    k_fw: float = None
    k_u: float = 0.95

    def __post_init__(self, par, w_m_nom, alpha_fw):
        # Minimum stator flux
        if self.psi_s_min is None:
            self.psi_s_min = par.psi_f
        # Field-weakening gain
        if self.k_fw is None:
            self.k_fw = alpha_fw/(w_m_nom*par.L_d)
        # Generate LUTs
        tq = TorqueCharacteristics(par)
        mtpa = tq.mtpa_locus(self.i_s_max, self.psi_s_min)
        lim = tq.mtpv_and_current_limits(self.i_s_max)
        # MTPA locus
        self.i_sd_mtpa = mtpa.i_sd_vs_tau_M
        # Merged MTPV and current limits
        self.tau_M_lim = lim.tau_M_vs_abs_psi_s
        self.i_sd_lim = lim.i_sd_vs_tau_M


# %%
class CurrentReference:
    """
    Current reference calculation.

    This method includes the MTPA locus and field-weakening operation based on
    the unlimited voltage reference feedback. The MTPV and current limits are
    taken into account. This resembles the method presented [#Bed2020]_.

    Parameters
    ----------
    par : ModelPars
        Machine model parameters.
    ref : CurrentReferencePars
        Reference generation parameters.
        
    Methods
    -------
    output(tau_M_ref, w_m, u_dc)
        Compute the stator current reference.
    update(T_s, tau_M_ref_lim, u_s_ref, u_dc)
        Field-weakening based on the unlimited reference voltage.

    Notes
    -----
    Instead of the PI controller used in [#Bed2020]_, we use a simpler integral
    controller with a constant gain. The resulting operating-point-dependent
    closed-loop pole could be derived using (12) of the paper. Unlike in 
    [#Bed2020]_, the MTPV limit is also included here by means of limiting the 
    reference torque and the d-axis current reference.

    References
    ----------
    .. [#Bed2020] Bedetti, Calligaro, Petrella, "Analytical design and 
       autotuning of adaptive flux-weakening voltage regulation loop in IPMSM 
       drives with accurate torque regulation," IEEE Trans. Ind. Appl., 2020,
       https://doi.org/10.1109/TIA.2019.2942807

    """

    # pylint: disable=too-many-instance-attributes, too-many-arguments
    def __init__(self, par, ref):
        # Machine model parameters
        self.n_p = par.n_p
        self.L_d, self.L_q, self.psi_f = par.L_d, par.L_q, par.psi_f
        # Reference generation parameters
        self.i_sd_mtpa = ref.i_sd_mtpa  # MTPA locus
        self.tau_M_lim = ref.tau_M_lim  # Merged MTPV and current limits
        self.i_sd_lim = ref.i_sd_lim  # Merged MTPV and current limits
        self.psi_s_min = ref.psi_s_min  # Minimum flux linkage
        self.i_s_max = ref.i_s_max  # Maximum current
        self.k_fw = ref.k_fw  # Field-weakening gain
        self.k_u = ref.k_u  # Voltage utilization factor
        # State
        self.i_sd_ref = 0

    def output(self, tau_M_ref, w_m, u_dc):
        """
        Compute the stator current reference.

        Parameters
        ----------
        tau_M_ref : float
            Torque reference (Nm).
        w_m : float
            Rotor speed (electrical rad/s)
        u_dc : float
            DC-bus voltage (V).

        Returns
        -------
        i_s_ref : complex
            Stator current reference (A).
        tau_M_ref_lim : float
            Limited torque reference (Nm).

        """

        def limit_torque(tau_M_ref, w_m, u_dc):
            if np.abs(w_m) > 0:
                psi_s_max = self.k_u*u_dc/np.sqrt(3)/np.abs(w_m)
                tau_M_max = self.tau_M_lim(psi_s_max)
            else:
                tau_M_max = self.tau_M_lim(np.inf)

            if np.abs(tau_M_ref) > tau_M_max:
                tau_M_ref = np.sign(tau_M_ref)*tau_M_max

            return tau_M_ref

        # Limit the torque reference according to MTPV and current limits
        tau_M_ref = limit_torque(tau_M_ref, w_m, u_dc)

        # q-axis current reference
        psi_t = self.psi_f + (self.L_d - self.L_q)*self.i_sd_ref
        i_sq_ref = tau_M_ref/(1.5*self.n_p*psi_t) if psi_t != 0 else 0

        # Limit the q-axis current reference
        i_sd_mtpa = self.i_sd_mtpa(np.abs(tau_M_ref))
        i_sq_max = np.min([
            np.sqrt(self.i_s_max**2 - self.i_sd_ref**2),
            np.sqrt(self.i_s_max**2 - i_sd_mtpa**2)
        ])
        if np.abs(i_sq_ref) > i_sq_max:
            i_sq_ref = np.sign(i_sq_ref)*i_sq_max

        # Current reference
        i_s_ref = self.i_sd_ref + 1j*i_sq_ref

        # Limited torque (for the speed controller)
        tau_M_ref_lim = 1.5*self.n_p*psi_t*i_sq_ref

        return i_s_ref, tau_M_ref_lim

    def update(self, T_s, tau_M_ref_lim, u_s_ref, u_dc):
        """
        Field-weakening based on the unlimited reference voltage.

        Parameters
        ----------
        T_s : float
            Sampling period (s).
        tau_M_ref_lim : float
            Limited torque reference (Nm).
        u_s_ref : complex
            Unlimited stator voltage reference (V).
        u_dc : float 
            DC-bus voltage (V).

        """
        u_s_max = self.k_u*u_dc/np.sqrt(3)
        self.i_sd_ref += T_s*self.k_fw*(u_s_max - np.abs(u_s_ref))

        # Limit the current
        i_sd_mtpa = self.i_sd_mtpa(np.abs(tau_M_ref_lim))
        i_sd_lim = self.i_sd_lim(np.abs(tau_M_ref_lim))
        self.i_sd_ref = np.clip(self.i_sd_ref, i_sd_lim, i_sd_mtpa)


# %%
class Observer:
    """
    Observer for synchronous machines.

    This observer estimates the rotor angle, the rotor speed, and the stator 
    flux linkage. The design is based on [#Hin2018]_. The observer gain 
    decouples the electrical and mechanical dynamics and allows placing the 
    poles of the corresponding linearized estimation error dynamics. This 
    implementation operates in estimated rotor coordinates. The observer can 
    also be used in the sensored mode by providing the measured rotor speed as 
    an input.

    Parameters
    ----------
    par : ModelPars
        Machine model parameters.
    alpha_o : float, optional
        Observer bandwidth (electrical rad/s). The default is 2*pi*40.
    k : callable, optional
        Observer gain as a function of the rotor angular speed. The default is 
        ``lambda w_m: 0.25*(R_s*(L_d + L_q)/(L_d*L_q) + 0.2*abs(w_m))`` if
        `sensorless` else ``lambda w_m: 2*pi*15``.

    Attributes
    ----------
    theta_m : float
        Rotor angle estimate (electrical rad).
    w_m : float
        Rotor speed estimate (electrical rad/s).
    psi_s : complex
        Stator flux estimate (Vs).
        
    Methods
    -------
    update(T_s, u_s, i_s, w_m=None)
        Update the observer state.

    References
    ----------
    .. [#Hin2018] Hinkkanen, Saarakkala, Awan, Mölsä, Tuovinen, "Observers for
       sensorless synchronous motor drives: Framework for design and analysis,"
       IEEE Trans. Ind. Appl., 2018, https://doi.org/10.1109/TIA.2018.2858753

    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, par, alpha_o=2*np.pi*40, k=None, sensorless=True):
        self.R_s = par.R_s
        self.L_d, self.L_q, self.psi_f = par.L_d, par.L_q, par.psi_f
        self.psi_f = par.psi_f
        self.alpha_o = alpha_o
        self.sensorless = sensorless
        self.k1 = k
        if self.sensorless:
            if self.k1 is None:  # If not given, use the default gains
                sigma0 = .25*par.R_s*(par.L_d + par.L_q)/(par.L_d*par.L_q)
                self.k1 = lambda w_m: (sigma0 + .2*np.abs(w_m))
            self.k2 = self.k1
        else:
            if self.k1 is None:
                self.k1 = lambda w_m: 2*np.pi*15
            self.k2 = 0*self.k1
        # Initial states
        self.theta_m, self.w_m, self.psi_s = 0, 0, par.psi_f

    def update(self, T_s, u_s, i_s, w_m=None):
        """
        Update the states for the next sampling period.

        Parameters
        ----------
        T_s : float
            Sampling period (s).
        u_s : complex
            Stator voltage (V) in estimated rotor coordinates.
        i_s : complex
            Stator current (A) in estimated rotor coordinates.
        w_m : float, optional
            Rotor angular speed (electrical rad/s). Needed only in the sensored
            mode. The default is None. 

        """
        # Estimation error
        e = self.L_d*i_s.real + 1j*self.L_q*i_s.imag + self.psi_f - self.psi_s

        if self.sensorless:
            # Auxiliary flux
            psi_a = self.psi_f + (self.L_d - self.L_q)*np.conj(i_s)

            # Observer gains
            k1 = self.k1(self.w_m)
            k2 = k1*psi_a/np.conj(psi_a) if np.abs(psi_a) > 0 else k1

            # Speed estimation
            eps = -np.imag(e/psi_a) if np.abs(psi_a) > 0 else 0
            w_s = 2*self.alpha_o*eps + self.w_m
        else:
            k1, k2 = self.k1, 0
            w_s = w_m
            eps = 0

        # Update the states
        self.psi_s += T_s*(
            u_s - self.R_s*i_s - 1j*w_s*self.psi_s + k1*e + k2*np.conj(e))
        self.w_m += T_s*self.alpha_o**2*eps
        self.theta_m += T_s*w_s  # Next line: limit into [-pi, pi)
        self.theta_m = np.mod(self.theta_m + np.pi, 2*np.pi) - np.pi
